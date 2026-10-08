"""Validate, propose, accept and render source-backed resumes without changing facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Application, Candidate, Job
from ..schemas.profile import Profile
from ..schemas.resume import Claim, ResumeContent, ResumeDraft, ResumeEntry, ResumePackage, ResumeRecord
from ..schemas.tracking import ReviewState
from . import profile as profile_service
from .claims import claim_problems, detection_terms
from .skills import canon, display
from .text import content_hash


class ResumeError(Exception):
    pass


class ResumeInvalid(ResumeError):
    def __init__(self, problems: list[str]):
        super().__init__("Resume rejected; nothing was saved. " + "; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class ResumeState:
    proposal: ResumeRecord | None
    accepted: ResumeRecord | None
    proposal_stale: bool
    accepted_stale: bool


def _dump(profile: Profile | dict) -> dict:
    return profile.model_dump() if isinstance(profile, Profile) else profile


def _shared_profile(context: dict) -> dict:
    """The outbound shape represented for the same local source checks."""
    return context | {
        "summary": (context.get("summary") or {}).get("text", ""),
        "skills": [{"name": name} for name in context.get("skills") or []],
    }


def _skill_labels(prof: dict) -> dict[str, str]:
    labels = {canon(s["name"]): display(s["name"]) for s in prof.get("skills") or []}
    for section in ("experience", "projects"):
        for entry in prof.get(section) or []:
            for tech in entry.get("technologies") or []:
                labels.setdefault(canon(tech), display(tech))
    return labels


def validate_resume(profile: Profile | dict, job: Job | dict, data, context: dict | None = None) -> tuple[ResumeContent, list[str]]:
    """Validate shape, canonical claims and (for AI) the exact shared content."""
    try:
        draft = ResumeDraft.model_validate(data.model_dump() if isinstance(data, ResumeDraft) else data)
    except ValidationError as exc:
        raise ResumeInvalid([
            f"{'.'.join(map(str, e['loc'])) or 'resume'}: {e['msg']}" for e in exc.errors()
        ]) from None
    prof = _dump(profile)
    sources = profile_service.sources(prof)
    entries = {section: {e["id"]: e for e in prof.get(section) or []} for section in ("experience", "projects", "education", "certifications")}
    shared = _shared_profile(context) if context is not None else prof
    shared_sources = profile_service.sources(shared)
    selection = {section: {e["id"] for e in shared.get(section) or []} for section in entries}
    extra = detection_terms(prof, job)
    errors, warnings = [], []

    def check_claim(claim: Claim, where: str, entry: dict | None = None):
        cited = claim.sources
        missing = [s for s in cited if s not in sources]
        excluded = [s for s in cited if s not in shared_sources]
        if missing:
            errors.append(f"{where}: unknown sources {', '.join(missing)}")
            return
        if entry is not None:
            foreign = [s for s in cited if sources[s].entry != entry["id"]]
            non_bullets = [s for s in cited if sources[s].kind != "bullet"]
            if foreign:
                errors.append(f"{where}: sources {', '.join(foreign)} belong to a different entry than {entry['id']}")
            if non_bullets:
                errors.append(f"{where}: cite source bullets, not entry IDs: {', '.join(non_bullets)}")
            if foreign or non_bullets:
                return
        if excluded:
            errors.append(f"{where}: sources not in the shared context: {', '.join(excluded)}")
            return
        allowed = {canon(t) for t in entry.get("technologies") or []} if entry else profile_service.skill_keys(prof)
        for problem in claim_problems(claim.text, [sources[s].text for s in cited], allowed, extra):
            errors.append(f"{where}: {problem}")
        if context is not None:
            shared_entry = next((e for section in ("experience", "projects") for e in shared.get(section) or [] if entry and e["id"] == entry["id"]), None)
            shared_allowed = {canon(t) for t in shared_entry.get("technologies") or []} if shared_entry else profile_service.skill_keys(shared)
            for problem in claim_problems(claim.text, [shared_sources[s].text for s in cited], shared_allowed, extra):
                errors.append(f"{where}: shared context {problem}")

    if draft.summary:
        check_claim(draft.summary, "summary")
    for section in ("experience", "projects"):
        seen = set()
        for i, item in enumerate(getattr(draft, section)):
            where = f"{section}[{i}]"
            entry = entries[section].get(item.entry)
            if entry is None:
                errors.append(f"{where}: {item.entry!r} is not a {section} entry in the profile")
                continue
            if item.entry not in selection[section]:
                errors.append(f"{where}: entry {item.entry} is not in the shared context")
                continue
            if item.entry in seen:
                errors.append(f"{where}: entry {item.entry} appears twice")
                continue
            seen.add(item.entry)
            label = (f"Experience: {entry.get('title') + ' at ' if entry.get('title') else ''}{entry['organization']}"
                     if section == "experience" else f"Project: {entry['name']}")
            for b, claim in enumerate(item.bullets):
                check_claim(claim, f"{where}.bullets[{b}]", entry)
                if len(claim.text) > 350:
                    warnings.append(f"{label}, bullet {b + 1}: long bullet ({len(claim.text)} characters)")
            if not item.bullets:
                warnings.append(f"{label} has no bullets")

    defaults = {}
    for section in ("education", "certifications"):
        requested = getattr(draft, section)
        requested = [e["id"] for e in shared.get(section) or []] if requested is None else requested
        for item_id in requested:
            if item_id not in entries[section]:
                errors.append(f"{section}: unknown entries {item_id}")
            elif item_id not in selection[section]:
                errors.append(f"{section}: entry {item_id} is not in the shared context")
        defaults[section] = list(dict.fromkeys(requested))
    allowed_skills, shared_skills = profile_service.skill_keys(prof), profile_service.skill_keys(shared)
    labels = _skill_labels(prof)
    requested_skills = [s["name"] for s in shared.get("skills") or []] if draft.skills is None else draft.skills
    chosen, keys = [], set()
    for name in requested_skills:
        key = canon(name)
        if key not in allowed_skills:
            errors.append(f"skills: {name!r} is not in the profile")
        elif key not in shared_skills:
            errors.append(f"skills: {name!r} is not in the shared context")
        elif key not in keys:
            chosen.append(labels[key])
            keys.add(key)
    defaults["skills"] = chosen
    if not draft.experience and not draft.projects:
        warnings.append("Proposal includes no experience or projects")
    if errors:
        raise ResumeInvalid(errors)
    return ResumeContent.model_validate(draft.model_dump() | defaults), warnings


def profile_draft(profile: Profile | dict) -> ResumeContent:
    """An editable proposal that quotes original profile bullets without AI."""
    prof = _dump(profile)
    data = {section: [ResumeEntry(entry=e["id"], bullets=[Claim(text=b["text"], sources=[b["id"]]) for b in e.get("bullets") or []])
                      for e in prof.get(section) or []] for section in ("experience", "projects")}
    return ResumeContent(
        summary=Claim(text=prof["summary"], sources=["summary"]) if prof.get("summary") else None,
        **data, education=[e["id"] for e in prof.get("education") or []],
        certifications=[c["id"] for c in prof.get("certifications") or []],
        skills=[s["name"] for s in prof.get("skills") or []],
    )


def _revision_guard(job: Job, candidate: Candidate, profile_revision: int, job_revision: int):
    return (
        exists(select(Candidate.id).where(Candidate.id == candidate.id, Candidate.revision == profile_revision)),
        exists(select(Job.id).where(Job.id == job.id, Job.revision == job_revision)),
    )


def propose(session: Session, job: Job, candidate: Candidate, data, model: str = "", prompt_revision: str = "",
            outbound_hash: str | None = None, expected_profile_revision: int | None = None,
            expected_job_revision: int | None = None, created_at: datetime | None = None,
            context: dict | None = None) -> ResumeRecord:
    expected_profile_revision = candidate.revision if expected_profile_revision is None else expected_profile_revision
    expected_job_revision = job.revision if expected_job_revision is None else expected_job_revision
    if expected_profile_revision != candidate.revision or expected_job_revision != job.revision:
        raise ResumeError("The profile or job changed while the resume was being prepared. Generate a fresh proposal.")
    content, warnings = validate_resume(candidate.profile, job, data, context)
    record = ResumeRecord(resume=content, profile_revision=expected_profile_revision, job_revision=expected_job_revision,
                          model=model, prompt_revision=prompt_revision, outbound_hash=outbound_hash,
                          created_at=created_at or utcnow(), warnings=warnings)
    with session.no_autoflush:
        result = session.execute(update(Application).where(
            Application.job_id == job.id, *_revision_guard(job, candidate, expected_profile_revision, expected_job_revision),
        ).values(current_proposal=record, review_state=ReviewState.DRAFT.value, updated_at=utcnow()).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        session.rollback()
        raise ResumeError("The profile or job changed while the resume was being prepared. Generate a fresh proposal.")
    session.commit()
    session.refresh(job.application)
    return record


def _record_token(record: ResumeRecord) -> str:
    return content_hash(record.model_dump(mode="json"))


def proposal_token(record: ResumeRecord) -> str:
    return _record_token(record)


def accept(session: Session, job: Job, candidate: Candidate, proposal_token: str) -> ResumePackage:
    record = job.application.current_proposal
    if record is None:
        raise ResumeError("No resume proposal to accept. Create a proposal first.")
    if proposal_token != _record_token(record):
        raise ResumeError("The proposal changed since you reviewed it. Review the current proposal before accepting.")
    if record.profile_revision != candidate.revision or record.job_revision != job.revision:
        raise ResumeError("The profile or job changed since this proposal was created. Generate a fresh proposal.")
    validate_resume(candidate.profile, job, record.resume)
    package = ResumePackage(resume=record, accepted_at=utcnow())
    with session.no_autoflush:
        result = session.execute(update(Application).where(
            Application.job_id == job.id, Application.current_proposal == record,
            *_revision_guard(job, candidate, record.profile_revision, record.job_revision),
        ).values(accepted_package=package, candidate_id=candidate.id, review_state=ReviewState.DRAFT.value,
                 updated_at=utcnow()).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        session.rollback()
        raise ResumeError("The proposal, profile or job changed since you reviewed it. Review a fresh proposal before accepting.")
    session.commit()
    session.refresh(job.application)
    return package


def state(job: Job, candidate: Candidate | None) -> ResumeState:
    proposal = job.application.current_proposal
    package = job.application.accepted_package
    accepted = package.resume if package else None

    def stale(record):
        if record is None:
            return False
        if candidate is None or record.profile_revision != candidate.revision or record.job_revision != job.revision:
            return True
        try:
            validate_resume(candidate.profile, job, record.resume)
        except ResumeInvalid:
            return True
        return False

    return ResumeState(proposal, accepted, stale(proposal), stale(accepted))


def render_context(profile: Profile | dict, content: ResumeContent) -> dict:
    """Use selected claims and trusted local facts; no model-supplied metadata."""
    prof = _dump(profile)
    sources = profile_service.sources(prof)

    def claim_data(claim):
        return claim.model_dump() | {"originals": [{"id": s, "text": sources[s].text} for s in claim.sources if s in sources]}

    labels = _skill_labels(prof)
    out = {"contact": prof.get("contact", {}), "summary": claim_data(content.summary) if content.summary else None,
           "skills": [labels[canon(s)] for s in content.skills if canon(s) in labels]}
    for section in ("experience", "projects"):
        entries = {e["id"]: e for e in prof.get(section) or []}
        out[section] = [{k: v for k, v in entries[item.entry].items() if k != "bullets"} |
                        {"entry": item.entry, "bullets": [claim_data(c) for c in item.bullets]}
                        for item in getattr(content, section) if item.entry in entries]
    for section in ("education", "certifications"):
        entries = {e["id"]: e for e in prof.get(section) or []}
        out[section] = [entries[i] for i in getattr(content, section) if i in entries]
    return out
