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

from sqlalchemy import String, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..db import utcnow
from ..models import Application, Candidate, Job
from ..schemas.package import Approval, SubmittedSnapshot
from ..schemas.tracking import ReviewState, TrackingStatus
from ..templating import templates
from . import answers as answer_service
from . import questions as q_rules
from . import resume as resume_service
from . import tracking, transactions
from .profile import get_candidate
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
    candidate and every job field frozen in a snapshot, with their revisions. Pending drafts,
    unaccepted resume proposals and tracking notes are left out: they don't change what would
    be approved or submitted.
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
        "job_details": {name: getattr(job, name) for name in ("title", "company", "location", "url", "description")},
    }))


def answer_blocker(item: answer_service.ResolvedAnswer) -> str | None:
    """Why an unresolved answer stops approval, or None when it is only a warning (an optional question)."""
    if item.resolved:
        return None
    question = item.question
    name = f'"{_short(question.text)}"'
    if item.problem:  # even when optional: the value is there, but can't be submitted as it is
        return f"{name} ({item.label}): {answer_service.too_long_message(question, item.problem)}"
    if question.category in q_rules.SENSITIVE_CATEGORIES:
        return (f"{name} ({item.label}): sensitive questions need your own answer, "
                "your confirmation or an explicit skip.")
    if question.category == q_rules.UNKNOWN:
        return f"{name}: confirm the question's category."
    if question.required:
        return f"{name} ({item.label}): this required question has no accepted answer."
    return None


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
        blocker = answer_blocker(item)
        if blocker:
            blockers.append(blocker)
        else:
            warnings.append(f'"{_short(item.question.text)}" ({item.label}): this optional question is unanswered.')
    if candidate is not None:
        for item in items:
            problems = answer_service.answer_problems(job, item.question, item.answer, candidate.profile)
            if problems:
                blockers.append(f'"{_short(item.question.text)}": the AI-drafted answer is no longer supported by '
                                f"your profile ({'; '.join(problems)}). Edit it, or draft and accept it again.")
    pending = [f'"{_short(a.question.text)}"' for a in items
               if a.draft is not None and a.question.category == q_rules.OPEN and not a.skipped]
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


# Raise this when approval checks get stricter. Approvals recorded under an older version are
# checked against the current rules (an unchanged package may no longer pass); approvals made
# under the current version passed these checks already, so reading their state stays cheap.
# 2: AI answers are supported only by the sources they cite.
APPROVAL_RULES = 2


def approval_key(approval: Approval) -> str:
    """Identifies one approval record, so a rules stamp can't carry over to a later approval."""
    return content_hash(approval.model_dump(mode="json"))


def _mark_checked(application: Application) -> None:
    """Record that the current approval passed the current rules."""
    application.approval_rules = APPROVAL_RULES
    application.approval_rules_for = approval_key(application.approval)


def _mark_seen(application: Application) -> None:
    """Record that startup looked at the current approval without vouching for it.

    Used for approvals that don't read Approved: nothing needs checking now, and the startup
    pass skips them next time. The marker differs from ``_mark_checked``, so if such an approval
    reads Approved again (an edit reverted), it is still checked when its state is read.
    """
    application.approval_rules = APPROVAL_RULES
    application.approval_rules_for = "seen:" + approval_key(application.approval)


def _settled(application: Application) -> bool:
    """Whether startup has nothing left to do for the current approval under the current rules."""
    key = approval_key(application.approval)
    return application.approval_rules >= APPROVAL_RULES and application.approval_rules_for in (key, "seen:" + key)


def _checked(application: Application) -> bool:
    """Whether the current approval is known to pass the current rules.

    The stamp must name this very approval: earlier versions of the app can clear an approval
    and record a new one under their own rules without touching the stamp columns.
    """
    approval = application.approval
    return (approval is not None and application.approval_rules >= APPROVAL_RULES
            and application.approval_rules_for == approval_key(approval))


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
        _mark_checked(application)
        application.review_state = ReviewState.APPROVED.value
        application.updated_at = utcnow()
    return approval


def rewrite_approvals_with_stored_rules(session: Session) -> int:
    """Rewrite approvals and snapshots that still carry the rules version inside their JSON.

    One release stored it there; earlier versions of the app reject the extra key. Reading
    already drops it, so saving the row again removes it from the database. Returns how many
    applications were rewritten.
    """
    stored = Application.approval.cast(String).like('%"rules":%') | \
        Application.submitted_snapshots.cast(String).like('%"rules":%')
    applications = session.scalars(select(Application).where(stored)).all()
    for application in applications:
        flag_modified(application, "approval")
        flag_modified(application, "submitted_snapshots")
    session.commit()
    return len(applications)


def _recorded_state(job: Job, application: Application, candidate: Candidate | None) -> ReviewState:
    """Draft, Approved or Stale from the approval record and the package as it is now."""
    approval = application.approval
    if approval is None:
        return ReviewState.DRAFT
    # Changed inputs read as Stale even when they also changed the content.
    if candidate is None or approval.profile_revision != candidate.revision or approval.job_revision != job.revision:
        return ReviewState.STALE
    if approval.content_hash != content_hash(resolved_package(job, application, candidate)):
        return ReviewState.DRAFT
    return ReviewState.APPROVED


def review_state(job: Job, application: Application, candidate: Candidate | None) -> ReviewState:
    """Draft, Approved or Stale, computed from what was approved and what the package is now."""
    state = _recorded_state(job, application, candidate)
    # An approval from older, looser rules: a matching hash doesn't make the package pass now.
    # upgrade_stored_approvals settles these at startup, so this check rarely runs.
    if state is ReviewState.APPROVED and not _checked(application) and check(job, application, candidate)[0]:
        return ReviewState.DRAFT
    return state


def upgrade_stored_approvals(session: Session) -> int:
    """Check approvals recorded under older rules once, at startup. Returns how many were settled.

    An approval that would read Approved and still passes the current checks is marked with
    the current rules version and its key, so reading its state stays cheap. One that no longer passes is
    removed and its package is a Draft again, as when its content changes. Stale and Draft
    approvals are left as they are, since they don't read Approved, and only marked as seen so
    later starts skip them.
    """
    candidate = get_candidate(session)
    settled = 0
    # Only rows with an approval can need settling. A cleared approval is stored as JSON null.
    approved = Application.approval.is_not(None) & (Application.approval.cast(String) != "null")
    for application in session.scalars(select(Application).where(approved)):
        if _settled(application):
            continue
        job = application.job
        if _recorded_state(job, application, candidate) is not ReviewState.APPROVED:
            _mark_seen(application)
            continue
        if check(job, application, candidate)[0]:
            application.approval = None
            application.review_state = ReviewState.DRAFT.value
            application.updated_at = utcnow()
        else:
            _mark_checked(application)
        settled += 1
    session.commit()
    return settled


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
                   note: str = "", today: date | None = None, follow_up_days: int | None = None) -> SubmittedSnapshot:
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
        tracking.apply_status(application, TrackingStatus.APPLIED.value, on=on, note=note, today=today,
                              follow_up_days=follow_up_days)
        application.submitted_snapshots = [*application.submitted_snapshots, snapshot]
        application.review_state = state.value
    return snapshot
