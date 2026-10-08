"""Generate a sourced resume proposal from the candidate's approved shared content.

Validation runs against both the canonical profile and the exact content sent. The
profile and accepted package are never edited by this operation. Only validated
results are cached, and result persistence and proposal creation share a transaction.
"""

from __future__ import annotations

from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import AIRun, Candidate, Job
from ..schemas.resume import ResumeRecord
from ..services import outbound
from ..services import resume as resume_service
from . import operations, prompts
from .client import AIError, OpenRouterClient, ResultProblem

TAILOR_RESUME = "tailor_resume"


def job_context(job: Job) -> dict:
    """Employer data for tailoring; it cannot provide candidate facts."""
    return operations.job_inputs(job) | {
        "requirements": [requirement.model_dump(mode="json", by_alias=True) for requirement in job.requirements],
    }


def _assert_current(session: Session, settings: Settings, job: Job, candidate: Candidate,
                    profile_revision: int, job_revision: int, context_hash: str) -> None:
    session.refresh(candidate)
    session.refresh(job)
    approval = outbound.get_approval(session, candidate)
    if approval is not None:
        session.refresh(approval)
    current = outbound.state(session, settings, candidate)
    if (candidate.revision != profile_revision or job.revision != job_revision
            or current.context_hash != context_hash):
        raise resume_service.ResumeError(
            "Your profile, job or sharing choices changed while the resume was being generated. "
            "Review the current information and generate again. Nothing was replaced.",
        )


def _store_run(session: Session, *, key: str, job: Job, candidate: Candidate, model: str,
               prompt_revision: str, context_hash: str, content: dict, usage: dict, attempts: int) -> AIRun:
    """Stage the cache row; propose() commits it together with the proposal."""
    session.execute(delete(AIRun).where(AIRun.cache_key == key))
    run = AIRun(
        operation=TAILOR_RESUME, cache_key=key, job_id=job.id, model=model,
        prompt_revision=prompt_revision,
        input_revisions={"job": job.revision, "profile": candidate.revision, "outbound": context_hash},
        result={"resume": content}, usage=usage, attempts=attempts, created_at=utcnow(),
    )
    session.add(run)
    session.flush()
    return run


def tailor_resume(session: Session, client: OpenRouterClient, settings: Settings, job: Job,
                  candidate: Candidate, force: bool = False) -> ResumeRecord:
    """Generate or reuse a validated proposal, leaving acceptance to the candidate."""
    with operations.exclusive(TAILOR_RESUME, job.id):
        state = outbound.state(session, settings, candidate)
        context = state.context
        context_hash = state.context_hash
        if context is None or context_hash is None:
            raise operations.ApprovalNeeded()
        profile_revision, job_revision = candidate.revision, job.revision
        prompt_revision = prompts.TAILOR_RESUME_REVISION
        inputs = {"context": context, "job": job_context(job)}
        key = operations.cache_key(TAILOR_RESUME, prompt_revision, client.model, inputs)

        def validate(arguments: dict) -> dict:
            try:
                content, _ = resume_service.validate_resume(candidate.profile, job, arguments, context=context)
            except resume_service.ResumeInvalid as exc:
                raise ResultProblem(exc.problems) from None
            return content.model_dump(mode="json")

        try:
            run = None if force else operations.find_run(session, key)
            if run is not None:
                try:
                    content = validate(run.result["resume"])
                except (ResultProblem, ValueError, KeyError) as exc:
                    problems = exc.problems if isinstance(exc, ResultProblem) else []
                    raise AIError("invalid", "The cached resume no longer passes validation. "
                                             "Generate again; your existing work is unchanged.",
                                  problems=problems) from None
                # A no-op write obtains the database writer lock before the final
                # sharing check, so another session cannot change approval mid-save.
                session.execute(update(AIRun).where(AIRun.id == run.id).values(cache_key=run.cache_key))
            else:
                result = client.structured(
                    TAILOR_RESUME, prompts.tailor_resume_messages(context, inputs["job"]),
                    prompts.TAILOR_RESUME_TOOL, validate,
                    before_attempt=lambda: _assert_current(
                        session, settings, job, candidate, profile_revision, job_revision, context_hash,
                    ),
                )
                _assert_current(session, settings, job, candidate, profile_revision, job_revision, context_hash)
                content = result.value
                run = _store_run(
                    session, key=key, job=job, candidate=candidate, model=client.model,
                    prompt_revision=prompt_revision, context_hash=context_hash,
                    content=content, usage=result.usage, attempts=result.attempts,
                )
            # New rows have acquired the same writer lock. Check again under that
            # lock before atomically storing the proposal and the validated run.
            _assert_current(session, settings, job, candidate, profile_revision, job_revision, context_hash)
            return resume_service.propose(
                session, job, candidate, content, model=run.model, prompt_revision=run.prompt_revision,
                outbound_hash=context_hash, expected_profile_revision=profile_revision,
                expected_job_revision=job_revision, created_at=run.created_at, context=context,
            )
        except Exception:
            session.rollback()
            raise
