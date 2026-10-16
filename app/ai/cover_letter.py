"""Draft a cover letter body from the candidate's approved shared content.

Follows ``app.ai.answers``: only the reduced, user-approved candidate context and the job's own
text are sent; freshness is rechecked before every request and before saving; validation runs
against both the canonical profile and the exact content shared; only validated results are
cached; and a failure leaves the letter, any earlier draft and everything else as they were.

The draft is a proposal. It becomes the letter only when the user accepts it, and it is
validated again then (``services.cover_letter.accept_draft``). Storing it never clears approval.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import AIRun, Candidate, Job
from ..schemas.package import CoverLetterDraft
from ..services import cover_letter as letter_service
from ..services import outbound
from ..services import profile as profile_service
from ..services import resume as resume_service
from ..services.claims import detection_terms
from . import operations, prompts
from .client import AIError, OpenRouterClient, ResultProblem
from .resume import job_context

DRAFT_COVER_LETTER = "draft_cover_letter"


class DraftedLetter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    sources: list[str]


def _assert_current(session: Session, settings: Settings, job: Job, candidate: Candidate, profile_revision: int,
                    job_revision: int, context_hash: str) -> None:
    session.refresh(candidate)
    session.refresh(job)
    session.refresh(job.application)
    approval = outbound.get_approval(session, candidate)
    if approval is not None:
        session.refresh(approval)
    current = outbound.state(session, settings, candidate)
    if candidate.revision != profile_revision or job.revision != job_revision or current.context_hash != context_hash:
        raise letter_service.CoverLetterError(
            "Your profile, job or sharing choices changed while the cover letter was being drafted. "
            "Review the current information and draft again. Nothing was saved.",
        )


def _store_run(session: Session, *, key: str, job: Job, candidate: Candidate, model: str, prompt_revision: str,
               context_hash: str, content: dict, usage: dict, attempts: int) -> AIRun:
    """Stage the cache row; draft_cover_letter() commits it together with the draft."""
    session.execute(delete(AIRun).where(AIRun.cache_key == key))
    run = AIRun(
        operation=DRAFT_COVER_LETTER, cache_key=key, job_id=job.id, model=model, prompt_revision=prompt_revision,
        input_revisions={"job": job.revision, "profile": candidate.revision, "outbound": context_hash},
        result={"letter": content}, usage=usage, attempts=attempts, created_at=utcnow(),
    )
    session.add(run)
    session.flush()
    return run


def draft_cover_letter(session: Session, client: OpenRouterClient, settings: Settings, job: Job,
                       candidate: Candidate, force: bool = False) -> AIRun:
    """Draft (or reuse a validated) cover letter body, leaving acceptance to the candidate."""
    with operations.exclusive(DRAFT_COVER_LETTER, job.id):
        state = outbound.state(session, settings, candidate)
        context, context_hash = state.context, state.context_hash
        if context is None or context_hash is None:
            raise operations.ApprovalNeeded()
        profile_revision, job_revision = candidate.revision, job.revision
        prompt_revision = prompts.DRAFT_COVER_LETTER_REVISION
        job_data = job_context(job)
        inputs = {"context": context, "job": job_data, "words": letter_service.WORD_LIMIT}
        key = operations.cache_key(DRAFT_COVER_LETTER, prompt_revision, client.model, inputs)

        shared_sources = profile_service.sources(resume_service._shared_profile(context))
        shared_ids = outbound.source_ids(context)
        extra = detection_terms(candidate.profile, job)

        def validate(arguments: dict) -> dict:
            parsed = DraftedLetter.model_validate(arguments)
            text, cited = parsed.text.strip(), list(dict.fromkeys(parsed.sources))
            outside = [s for s in cited if s not in shared_ids]
            if outside:
                raise ResultProblem([f"sources not in the candidate content: {', '.join(outside)}"])
            problems = letter_service.validate_letter(candidate.profile, job, text, cited)
            if not problems:  # also check the exact (possibly reworded) content that was shared
                problems = [f"shared content: {p}" for p in letter_service.claim_problems(
                    text, shared_sources, cited, job, extra,
                )]
            if problems:
                raise ResultProblem(problems)
            return {"text": text, "sources": cited}

        def current() -> None:
            _assert_current(session, settings, job, candidate, profile_revision, job_revision, context_hash)

        try:
            run = None if force else operations.find_run(session, key)
            if run is not None:
                try:
                    content = validate(run.result["letter"])
                except (ResultProblem, ValueError, KeyError, TypeError) as exc:
                    problems = exc.problems if isinstance(exc, ResultProblem) else []
                    raise AIError("invalid", "The cached cover letter no longer passes validation. "
                                             "Draft again; your existing work is unchanged.",
                                  problems=problems) from None
                # A no-op write obtains the database writer lock before the final check.
                session.execute(update(AIRun).where(AIRun.id == run.id).values(cache_key=run.cache_key))
            else:
                result = client.structured(
                    DRAFT_COVER_LETTER,
                    prompts.draft_cover_letter_messages(context, job_data, letter_service.WORD_LIMIT),
                    prompts.DRAFT_COVER_LETTER_TOOL, validate, before_attempt=current,
                )
                current()
                content = result.value
                run = _store_run(
                    session, key=key, job=job, candidate=candidate, model=client.model,
                    prompt_revision=prompt_revision, context_hash=context_hash,
                    content=content, usage=result.usage, attempts=result.attempts,
                )
            # The writer lock is held now. Check again, then save the draft and run together.
            current()
            letter_service.store_draft(job.application, CoverLetterDraft(
                text=content["text"], sources=content["sources"], model=run.model,
                prompt_revision=run.prompt_revision, outbound_hash=context_hash,
                profile_revision=profile_revision, job_revision=job_revision, created_at=run.created_at,
            ))
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
