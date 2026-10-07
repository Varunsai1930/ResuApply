"""Deterministic requirement checklist: Met, Unmet or Unknown, always with its basis.

Ported from ResuSkill's ``resuskill_core.checklist``. Python decides every status; the
model only proposes requirements and evidence, and the user confirms both.

- Met: confirmed profile evidence satisfies the requirement.
- Unmet: confirmed profile evidence conflicts with it.
- Unknown: information is missing, ambiguous or cannot be compared reliably.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Candidate, Job
from ..schemas.profile import Profile
from ..schemas.requirements import DEGREE_LEVELS, EvidenceLink, Override
from . import profile as profile_service
from .countries import normalize_country
from .requirements import degree_level, excerpt_found, requirement
from .skills import canon
from .text import content_hash, date_key, norm_text, today_key

MET, UNMET, UNKNOWN = "met", "unmet", "unknown"
STATUSES = (MET, UNMET, UNKNOWN)
LABELS = {MET: "Met", UNMET: "Unmet", UNKNOWN: "Unknown"}


class ChecklistError(Exception):
    pass


@dataclass(frozen=True)
class Evidence:
    id: str
    text: str


@dataclass
class CheckResult:
    id: str
    text: str
    category: str
    importance: str
    excerpt: str
    status: str
    basis: str
    computed_status: str
    evidence: list[Evidence] = field(default_factory=list)
    stale_links: list[str] = field(default_factory=list)  # linked IDs no longer in the profile
    changed_evidence: list[Evidence] = field(default_factory=list)  # needs confirmation against current content
    override: Override | None = None
    excerpt_found: bool = True  # False when the description was edited and no longer contains it
    has_criterion: bool = False

    @property
    def label(self) -> str:
        return LABELS[self.status]


# ---------------------------------------------------------------- checks (dict-based, as in ResuSkill)

def _skill(crit: dict, prof: dict) -> tuple[str, str]:
    have = profile_service.skill_keys(prof)
    absent = {canon(s) for s in prof.get("skills_absent") or []}
    present = [s for s in crit["skills"] if canon(s) in have]
    lacking = [s for s in crit["skills"] if canon(s) in absent and canon(s) not in have]
    missing = [s for s in crit["skills"] if s not in present and s not in lacking]
    if crit["match"] == "any":
        if present:
            return MET, f"Profile lists {', '.join(present)}"
        if len(lacking) == len(crit["skills"]):
            return UNMET, f"You confirmed you don't have {', '.join(lacking)}"
        return UNKNOWN, f"Not in profile: {', '.join(missing)}"
    if len(present) == len(crit["skills"]):
        return MET, f"Profile lists {', '.join(present)}"
    if lacking:
        return UNMET, f"You confirmed you don't have {', '.join(lacking)}"
    return UNKNOWN, f"Not in profile: {', '.join(missing)}"


def _phrase_in(needle: str, haystack: str) -> bool:
    """True when ``needle`` appears in ``haystack`` as whole words (both normalized)."""
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def _field_matches(field_name: str, wanted: list[str]) -> bool:
    if not wanted:
        return True
    field_n = norm_text(field_name)
    return bool(field_n) and any(_phrase_in(norm_text(w), field_n) or _phrase_in(field_n, norm_text(w)) for w in wanted)


def _degree(crit: dict, prof: dict) -> tuple[str, str]:
    education = prof.get("education") or []
    if not education:
        return UNKNOWN, "No education in profile"
    needed = DEGREE_LEVELS.index(crit["level"])
    levels = []
    for entry in education:
        level = degree_level(entry.get("degree", ""))
        levels.append(level)
        if level is None or level < needed:
            continue
        end = entry.get("end")
        today = today_key()
        start_latest = date_key(entry.get("start"), end_of_period=True)
        end_earliest, end_latest = date_key(end), date_key(end, end_of_period=True)
        # Every possible start must be in the past and every possible end in the
        # future before partial dates can establish current enrollment.
        pursuing = bool(
            (end == "present" and (not entry.get("start") or (start_latest and start_latest <= today)))
            or (start_latest and start_latest <= today and end_earliest and end_earliest > today)
        )
        if start_latest and start_latest > today:
            continue  # planned or ambiguous enrollment is not a degree already held/pursued
        if crit["status"] == "pursuing" and not pursuing:
            continue
        if crit["status"] == "completed" and (end == "present" or not end_latest or end_latest > today):
            continue
        if not _field_matches(entry.get("field", ""), crit["fields"]):
            continue
        label = f"{entry.get('degree', '')} {entry.get('field', '')}".strip()
        return MET, f"{label} at {entry.get('institution', '')} ({entry['id']})"
    if None not in levels and max(levels) < needed:
        return UNMET, f"Highest degree in profile is below {crit['level']}"
    return UNKNOWN, "Degree level, field or status cannot be confirmed from the profile"


def _graduation(crit: dict, prof: dict) -> tuple[str, str]:
    grad = profile_service.graduation_date(prof)
    if not grad:
        return UNKNOWN, "No graduation date in profile"
    # A partial date ("2026") spans a period; only a comparison that holds for the whole period counts.
    earliest, latest = date_key(grad), date_key(grad, end_of_period=True)
    if earliest is None or latest is None:
        return UNKNOWN, "Graduation date is not a valid calendar date"
    if crit.get("from"):
        start = date_key(crit["from"])
        if start is None:
            return UNKNOWN, "Graduation window is not a valid calendar date"
        if latest < start:
            return UNMET, f"Graduation {grad} is before {crit['from']}"
        if earliest < start:
            return UNKNOWN, f"Graduation {grad} is not precise enough to compare with {crit['from']}"
    if crit.get("to"):
        end = date_key(crit["to"], end_of_period=True)
        if end is None:
            return UNKNOWN, "Graduation window is not a valid calendar date"
        if earliest > end:
            return UNMET, f"Graduation {grad} is after {crit['to']}"
        if latest > end:
            return UNKNOWN, f"Graduation {grad} is not precise enough to compare with {crit['to']}"
    return MET, f"Graduation {grad} is inside the window"


def _location(crit: dict, prof: dict) -> tuple[str, str]:
    prefs = prof.get("preferences") or {}
    mode = prefs.get("work_mode")
    results: list[tuple[str, str]] = []
    if crit.get("work_mode"):
        wanted = crit["work_mode"]
        if not mode:
            results.append((UNKNOWN, "No work-mode preference in profile"))
        elif mode == "any" or mode == wanted:
            results.append((MET, f"Profile work mode '{mode}' fits '{wanted}'"))
        elif mode == "remote" and wanted in ("onsite", "hybrid"):
            results.append((UNMET, f"Profile wants remote only; job is {wanted}"))
        else:
            results.append((UNKNOWN, f"Profile prefers '{mode}'; job is '{wanted}'"))
    if crit.get("locations"):
        places = [norm_text(p) for p in prefs.get("locations") or []]
        if (prof.get("contact") or {}).get("location"):
            places.append(norm_text(prof["contact"]["location"]))
        hits = [loc for loc in crit["locations"] if any(_phrase_in(norm_text(loc), p) or _phrase_in(p, norm_text(loc)) for p in places if p)]
        if hits:
            results.append((MET, f"Profile location/preferences include {', '.join(hits)}"))
        else:
            results.append((UNKNOWN, "Location not in profile preferences (relocation unknown)"))
    if any(s == UNMET for s, _ in results):
        return next(r for r in results if r[0] == UNMET)
    if results and all(s == MET for s, _ in results):
        return MET, "; ".join(b for _, b in results)
    return UNKNOWN, "; ".join(b for s, b in results if s != MET) or "Not comparable"


def _authorization(crit: dict, prof: dict) -> tuple[str, str]:
    try:
        country = normalize_country(crit["country"])
    except ValueError:
        return UNKNOWN, "Work authorization country is not recognized"
    record = None
    for item in prof.get("authorization") or []:
        try:
            if normalize_country(item["country"]) == country:
                record = item
                break
        except ValueError:
            continue  # unsupported legacy values cannot establish authorization
    if not record or record.get("authorized") is None:
        return UNKNOWN, f"Work authorization for {country} is not recorded"
    sponsor = crit.get("sponsorship_available")
    needs = record.get("requires_sponsorship")
    if record["authorized"]:
        if needs and sponsor is False:
            return UNMET, "Profile requires sponsorship; job offers none"
        if needs and sponsor is None:
            return UNKNOWN, "Profile requires sponsorship; job does not say if it sponsors"
        if needs is None:
            return UNKNOWN, "Sponsorship need is not recorded"
        return MET, f"Authorized to work in {country}"
    if sponsor is True:
        return MET, "Not yet authorized, but the job offers sponsorship"
    if sponsor is False:
        return UNMET, f"Not authorized in {country} and the job offers no sponsorship"
    return UNKNOWN, "Not authorized; job does not say if it sponsors"


def _availability(crit: dict, prof: dict) -> tuple[str, str]:
    start = (prof.get("availability") or {}).get("start_date")
    if not start:
        return UNKNOWN, "No start date in profile"
    earliest, latest = date_key(start), date_key(start, end_of_period=True)
    if earliest is None or latest is None:
        return UNKNOWN, "Start date is not a valid calendar date"
    if crit.get("start_by"):
        deadline = date_key(crit["start_by"], end_of_period=True)
        if deadline is None:
            return UNKNOWN, "Availability deadline is not a valid calendar date"
        if earliest > deadline:
            return UNMET, f"Available from {start}, after {crit['start_by']}"
        if latest > deadline:
            return UNKNOWN, f"Start date {start} is not precise enough to compare with {crit['start_by']}"
    return MET, f"Available from {start}"


CHECKS = {
    "skill": _skill,
    "degree": _degree,
    "graduation_window": _graduation,
    "location": _location,
    "authorization": _authorization,
    "availability": _availability,
}


def _source_hashes(profile: Profile | dict | None) -> dict[str, str]:
    """Bind confirmations to source content, including its relevant entry context."""
    prof = profile.model_dump() if isinstance(profile, Profile) else profile or {}
    hashes = {}
    if prof.get("summary"):
        hashes["summary"] = content_hash({"summary": prof["summary"]})
    for section in ("education", "experience", "projects"):
        for entry in prof.get(section) or []:
            hashes[entry["id"]] = content_hash({"section": section, "entry": entry})
            context = {k: v for k, v in entry.items() if k != "bullets"}
            for bullet in entry.get("bullets") or []:
                hashes[bullet["id"]] = content_hash({"section": section, "entry": context, "bullet": bullet})
    for cert in prof.get("certifications") or []:
        hashes[cert["id"]] = content_hash({"certification": cert})
    return hashes


def confirmed_source_ids(link: EvidenceLink | None, profile: Profile | dict | None) -> list[str]:
    """Current confirmed source IDs; legacy links require an explicit confirmation."""
    if link is None:
        return []
    hashes = _source_hashes(profile)
    return [i for i in link.sources if i in hashes and link.source_hashes.get(i) == hashes[i]]


def evaluate(job: Job, profile: Profile | None) -> list[CheckResult]:
    """The checklist for a job against the current profile. Without a profile everything is Unknown."""
    prof = profile.model_dump() if profile else {}
    sources = profile_service.sources(prof)
    hashes = _source_hashes(prof)
    results = []
    for req in job.requirements:
        link = job.evidence.get(req.id)
        linked = link.sources if link else []
        confirmed = [i for i in linked if i in hashes and link.source_hashes.get(i) == hashes[i]]
        evidence = [Evidence(i, sources[i].text) for i in confirmed]
        stale = [i for i in linked if i not in sources]
        changed = [Evidence(i, sources[i].text) for i in linked if i in sources and i not in confirmed]
        crit = req.criterion_dict()
        if not prof:
            status, basis = UNKNOWN, "No profile saved yet"
        elif crit and crit["type"] in CHECKS:
            status, basis = CHECKS[crit["type"]](crit, prof)
        elif crit and crit["type"] == "years_experience":
            status, basis = UNKNOWN, "Years of experience need confirmed evidence"
        else:
            status, basis = UNKNOWN, "No comparable criterion; needs confirmed evidence"
        if status == UNKNOWN and evidence:
            status, basis = MET, "You confirmed supporting evidence"
        elif status == UNKNOWN and changed:
            basis = "Linked evidence changed or needs confirmation; review and confirm the current content"
        computed = status
        override = job.overrides.get(req.id)
        if override:
            basis = f"Your override: {override.reason} (computed: {LABELS[computed]})"
            status = override.status
        results.append(CheckResult(
            id=req.id, text=req.text, category=req.category, importance=req.importance, excerpt=req.excerpt,
            status=status, basis=basis, computed_status=computed, evidence=evidence, stale_links=stale,
            changed_evidence=changed,
            override=override, excerpt_found=excerpt_found(job, req), has_criterion=crit is not None,
        ))
    return results


def summary(results: list[CheckResult]) -> dict[str, int]:
    counts = {s: 0 for s in STATUSES}
    for r in results:
        counts[r.status] += 1
    counts["required_unmet"] = sum(1 for r in results if r.status == UNMET and r.importance == "required")
    return counts


# ---------------------------------------------------------------- user actions

def _require(job: Job, req_id: str):
    req = requirement(job, req_id)
    if req is None:
        raise ChecklistError(f"This job has no requirement {req_id!r}.")
    return req


def link(session: Session, job: Job, candidate: Candidate | None, req_id: str, source_ids: list[str]) -> None:
    """Record evidence the user confirmed for a requirement."""
    _require(job, req_id)
    if candidate is None:
        raise ChecklistError("Create your profile before linking evidence.")
    source_ids = [s.strip() for s in source_ids if s and s.strip()]
    if not source_ids:
        raise ChecklistError("Choose at least one profile item as evidence.")
    known = profile_service.sources(candidate.profile)
    unknown = [s for s in source_ids if s not in known]
    if unknown:
        raise ChecklistError(f"Unknown profile item(s): {', '.join(unknown)}.")
    current = job.evidence.get(req_id) or EvidenceLink()
    merged = list(dict.fromkeys([*current.sources, *source_ids]))
    rejected = [s for s in current.rejected if s not in source_ids]
    hashes = _source_hashes(candidate.profile)
    confirmed_hashes = current.source_hashes | {i: hashes[i] for i in source_ids}
    job.evidence = job.evidence | {req_id: EvidenceLink(
        sources=merged, source_hashes=confirmed_hashes, rejected=rejected,
        confirmed_at=utcnow(), profile_revision=candidate.revision,
    )}
    job.updated_at = utcnow()
    session.commit()


def unlink(session: Session, job: Job, req_id: str, source_id: str) -> None:
    """Remove one confirmed evidence link."""
    _require(job, req_id)
    current = job.evidence.get(req_id)
    if current is None or source_id not in current.sources:
        raise ChecklistError("That evidence is not linked to this requirement.")
    remaining = [s for s in current.sources if s != source_id]
    evidence = dict(job.evidence)
    if remaining or current.rejected:
        evidence[req_id] = current.model_copy(update={
            "sources": remaining, "source_hashes": {k: v for k, v in current.source_hashes.items() if k != source_id},
        })
    else:
        evidence.pop(req_id)
    job.evidence = evidence
    job.updated_at = utcnow()
    session.commit()


def reject_suggestion(session: Session, job: Job, req_id: str, source_id: str) -> None:
    """Remember that the user turned down a suggested source, so it isn't offered again."""
    _require(job, req_id)
    current = job.evidence.get(req_id) or EvidenceLink()
    if source_id in current.sources:
        raise ChecklistError("That item is already linked; unlink it instead.")
    if source_id not in current.rejected:
        job.evidence = job.evidence | {req_id: current.model_copy(update={"rejected": [*current.rejected, source_id]})}
        job.updated_at = utcnow()
        session.commit()


def set_override(session: Session, job: Job, req_id: str, status: str, reason: str) -> None:
    """Set a manual status with a recorded explanation, or clear it with status "clear"."""
    _require(job, req_id)
    if status == "clear":
        if req_id in job.overrides:
            job.overrides = {k: v for k, v in job.overrides.items() if k != req_id}
            job.updated_at = utcnow()
            session.commit()
        return
    if status not in STATUSES:
        raise ChecklistError("Choose Met, Unmet or Unknown.")
    reason = (reason or "").strip()
    if not reason:
        raise ChecklistError("An override needs a reason.")
    if len(reason) > 1000:
        raise ChecklistError("Keep the reason under 1,000 characters.")
    job.overrides = job.overrides | {req_id: Override(status=status, reason=reason, at=utcnow())}
    job.updated_at = utcnow()
    session.commit()
