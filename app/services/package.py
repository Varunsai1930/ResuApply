"""Package approval, review state and submitted snapshots.

Ported from ResuSkill's ``resuskill_core.package`` (``resolved``, ``check``, ``approve``,
``review_state``) and ``track`` (``snapshot``), adapted to the database.

- The *package* is the accepted resume plus every question with its resolved answer.
  ``resolved_package`` is its JSON view; its hash is what approval records.
- Review state is computed: Draft (not approved, or content edited since), Stale (the profile
  or job changed since approval) or Approved. Approval never changes the tracking status.
- Recording Applied with an approved package stores a submitted snapshot: the job, the resume
  as rendered, the answers and the profile as they were. Nothing later changes it.
- Approving and recording Applied act on what the user reviewed: the page sends
  ``package_token`` and, under the writer lock (``transactions.write``), a package that no
  longer matches it is refused with ``PackageChanged``.

JSON columns are treated as immutable: every change assigns a new value.
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy.orm import Session

from ..db import utcnow
from ..models import Application, Candidate, Job
from ..schemas.package import Approval, SubmittedSnapshot
from ..schemas.tracking import ReviewState, TrackingStatus
from ..templating import templates
from . import answers as answer_service
from . import questions as q_rules
from . import resume as resume_service
from . import tracking, transactions
from .text import content_hash

RESUME_DOCUMENT_TEMPLATE = "_resume_document.html"
SHORT_QUESTION = 60


class PackageError(Exception):
    """A user-facing error. Nothing was changed."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


class PackageChanged(PackageError):
    """The package is not the one the user reviewed, or the page sent no token. Nothing was changed."""


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SHORT_QUESTION else text[:SHORT_QUESTION - 1].rstrip() + "…"


def _jsonable(data):
    """A deep, plain-JSON copy, so nothing in a stored package shares objects with the live data."""
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def _resolved_answers(job: Job, application: Application, candidate: Candidate | None):
    if candidate is None:
        return []
    return answer_service.resolve_all(job, application, candidate.profile)


def _resume_context(job: Job, application: Application, candidate: Candidate | None) -> dict | None:
    package = application.accepted_package
    if package is None or candidate is None:
        return None
    return resume_service.render_context(candidate.profile, package.resume.resume)


def _answer_entry(item: answer_service.ResolvedAnswer) -> dict:
    """What the package says about one question. A pending AI draft is not part of the package."""
    pending = item.label == answer_service.LABELS["pending"]
    return {
        "id": item.question.id,
        "question": item.question.text,
        "required": item.question.required,
        "category": item.question.category,
        "label": answer_service.LABELS["missing"] if pending else item.label,
        "text": "" if pending else item.text,
        "resolved": item.resolved,
        "skipped": item.skipped,
    }


def resolved_package(job: Job, application: Application, candidate: Candidate | None) -> dict:
    """The JSON view of the package: the accepted resume as rendered, and every question resolved.

    Pending AI drafts are shown as missing, so storing or discarding a draft doesn't change it.
    """
    context = _resume_context(job, application, candidate)
    return _jsonable({
        "resume": context,
        "questions": [_answer_entry(item) for item in _resolved_answers(job, application, candidate)],
    })


def package_token(job: Job, application: Application, candidate: Candidate | None) -> str:
    """What an approve or record button vouches for.

    The resolved package, the question definitions, the accepted resume record, and the
    candidate and job with their revisions. Pending drafts, unaccepted resume proposals and
    tracking notes are left out: they don't change what would be approved or submitted.
    """
    package = application.accepted_package
    return content_hash(_jsonable({
        "package": resolved_package(job, application, candidate),
        "questions": [q.model_dump(mode="json") for q in job.questions],
        "resume": package.model_dump(mode="json") if package else None,
        "candidate": candidate.id if candidate else None,
        "profile_revision": candidate.revision if candidate else None,
        "job": job.id,
        "job_revision": job.revision,
    }))


def check(job: Job, application: Application, candidate: Candidate | None) -> tuple[list[str], list[str]]:
    """Blockers (approval refused) and warnings (approval recorded with them) for the package."""
    blockers: list[str] = []
    warnings: list[str] = []
    if candidate is None:
        blockers.append("Create your profile first. The package is built from it.")
    current = resume_service.state(job, candidate)
    if current.accepted is None:
        blockers.append("No accepted resume. Prepare a resume, review it and accept it.")
    elif current.accepted_stale and candidate is not None:
        blockers.append("The accepted resume is out of date: the profile or job changed, or it no longer matches "
                        "the profile. Prepare a fresh resume, review it and accept it.")
    items = _resolved_answers(job, application, candidate)
    for item in items:
        if item.resolved:
            continue
        question = item.question
        name = f'"{_short(question.text)}"'
        if question.category in q_rules.SENSITIVE_CATEGORIES:
            blockers.append(f"{name} ({item.label}): sensitive questions need your own answer, "
                            "your confirmation or an explicit skip.")
        elif question.category == q_rules.UNKNOWN:
            blockers.append(f"{name}: confirm the question's category.")
        elif question.required:
            blockers.append(f"{name} ({item.label}): this required question has no accepted answer.")
        else:
            warnings.append(f"{name} ({item.label}): this optional question is unanswered.")
    if candidate is not None:
        for item in items:
            problems = answer_service.answer_problems(job, item.question, item.answer, candidate.profile)
            if problems:
                blockers.append(f'"{_short(item.question.text)}": the AI-drafted answer is no longer supported by '
                                f"your profile ({'; '.join(problems)}). Edit it, or draft and accept it again.")
    pending = [f'"{_short(a.question.text)}"' for a in items if a.label == answer_service.LABELS["pending"]]
    if pending:
        warnings.append(f"AI answer drafts waiting for your review: {', '.join(pending)}.")
    package = application.accepted_package
    proposal = current.proposal
    if package is not None and proposal is not None and proposal.created_at > package.accepted_at:
        warnings.append("A newer resume proposal exists that you haven't accepted.")
    if not job.requirements:
        warnings.append("No requirements were reviewed for this job.")
    return blockers, warnings


def _check_token(job: Job, application: Application, candidate: Candidate | None, token: str) -> None:
    if token != package_token(job, application, candidate):
        raise PackageChanged(
            "The package changed since you reviewed it, so nothing was recorded. "
            "Review the package as it is now, then try again.",
        )


def approve(session: Session, job: Job, candidate: Candidate | None, token: str) -> Approval:
    """Approve the package the user reviewed. Refused while there are blockers; never changes tracking status.

    ``token`` is ``package_token`` as the page showed it.
    """
    with transactions.write(session, job, candidate):
        application = job.application
        blockers, warnings = check(job, application, candidate)
        if blockers:
            raise PackageError("The package can't be approved yet.", blockers)
        _check_token(job, application, candidate, token)
        approval = Approval(
            content_hash=content_hash(resolved_package(job, application, candidate)),
            approved_at=utcnow(),
            profile_revision=candidate.revision,
            job_revision=job.revision,
            warnings=warnings,
        )
        application.approval = approval
        application.review_state = ReviewState.APPROVED.value
        application.updated_at = utcnow()
    return approval


def review_state(job: Job, application: Application, candidate: Candidate | None) -> ReviewState:
    """Draft, Approved or Stale, computed from what was approved and what the package is now."""
    approval = application.approval
    if approval is None:
        return ReviewState.DRAFT
    # Changed inputs read as Stale even when they also changed the content.
    if candidate is None or approval.profile_revision != candidate.revision or approval.job_revision != job.revision:
        return ReviewState.STALE
    if approval.content_hash != content_hash(resolved_package(job, application, candidate)):
        return ReviewState.DRAFT
    return ReviewState.APPROVED


def sync_review_state(session: Session, job: Job, application: Application,
                      candidate: Candidate | None) -> ReviewState:
    """Compute the review state and store it in the column when it differs, so lists can rely on it.

    Only for pages shown after a successful request. An error page uses ``review_state`` alone,
    so a refused action never writes anything.
    """
    state = review_state(job, application, candidate)
    if application.review_state != state.value:
        application.review_state = state.value
        session.commit()
    return state


# ---------------------------------------------------------------- submitted snapshots

def _resume_html(context: dict) -> str:
    """The A4 resume document as a standalone string: content only, nothing request-specific."""
    return templates.get_template(RESUME_DOCUMENT_TEMPLATE).render(rendered=context)


def get_snapshot(application: Application, snapshot_id: int) -> SubmittedSnapshot | None:
    return next((s for s in application.submitted_snapshots if s.id == snapshot_id), None)


def record_applied(session: Session, job: Job, candidate: Candidate | None, token: str, on: date | None = None,
                   note: str = "", today: date | None = None) -> SubmittedSnapshot:
    """Record Applied with a snapshot of the approved package the user reviewed, in one transaction.

    ``token`` is ``package_token`` as the page showed it. Only a current approved package can be
    frozen. Recording Applied without a package (the user applied some other way) is
    ``tracking.change_status`` on its own.
    """
    with transactions.write(session, job, candidate):
        application = job.application
        state = review_state(job, application, candidate)
        if state != ReviewState.APPROVED:
            raise PackageError(
                f"The package is {state.label}. Approve the current package before recording the application "
                "with it, or record Applied without a package.",
            )
        _check_token(job, application, candidate, token)
        today = today or date.today()
        context = _resume_context(job, application, candidate)
        if context is None:  # approval implies an accepted resume; stay safe if the data says otherwise
            raise PackageError("There is no accepted resume to freeze. Accept a resume and approve the package again.")
        snapshot = SubmittedSnapshot(
            id=max((s.id for s in application.submitted_snapshots), default=0) + 1,
            submitted_on=on or today,
            recorded_at=utcnow(),
            approval=application.approval,
            profile_revision=candidate.revision,
            job_revision=job.revision,
            job={"title": job.title, "company": job.company, "location": job.location, "url": job.url,
                 "description": job.description, "revision": job.revision},
            resume=_jsonable(context),
            resume_html=_resume_html(context),
            answers=[{k: entry[k] for k in ("question", "required", "category", "label", "text")}
                     for entry in resolved_package(job, application, candidate)["questions"]],
            profile=candidate.profile.model_copy(deep=True),
        )
        # The status change is validated first; the snapshot and the status are committed together.
        tracking.apply_status(application, TrackingStatus.APPLIED.value, on=on, note=note, today=today)
        application.submitted_snapshots = [*application.submitted_snapshots, snapshot]
        application.review_state = state.value
    return snapshot
