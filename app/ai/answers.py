"""Draft answers to open application questions from the candidate's approved shared content.

Follows the pattern of ``app.ai.resume``: only the reduced, user-approved candidate context
and the job's own text are sent; freshness is rechecked before every request and before
saving; validation is all-or-nothing and runs against both the canonical profile and the
exact content shared; only validated results are cached; and a failure leaves answers,
drafts and everything else exactly as they were.

A draft is a proposal. It becomes the answer only when the user accepts it, and it is
validated again against the profile as it is then (``services.answers.accept_draft``).
Storing drafts never clears approval: drafts are not package content.

An accepted AI answer that the current profile no longer supports is drafted again like an
unanswered question. The accepted answer stays as it is until the user accepts the replacement.
The model request runs outside any writer transaction; only the final check and save hold it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import utcnow
from ..models import AIRun, Candidate, Job
from ..schemas.package import AnswerDraft
from ..services import answers as answer_service
from ..services import outbound
from ..services import profile as profile_service
from ..services import resume as resume_service
from ..services.answers import AnswerError
from ..services.claims import detection_terms
from . import operations, prompts
from .client import AIError, OpenRouterClient, ResultProblem
from .operations import NothingToDo
from .resume import job_context

DRAFT_ANSWERS = "draft_answers"


class _Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    text: str
    sources: list[str]


class DraftedAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[_Answer]


def target_payload(job: Job, application, profile) -> list[dict]:
    """The questions sent to the model: ``services.answers.draft_targets`` against this profile."""
    return [
        {"id": q.id, "text": q.text, "limit": q.limit, "unit": q.limit_unit}
        for q in answer_service.draft_targets(job, application, profile)
    ]


def _assert_current(session: Session, settings: Settings, job: Job, candidate: Candidate, profile_revision: int,
                    job_revision: int, context_hash: str, targets: list[dict]) -> None:
    session.refresh(candidate)
    session.refresh(job)
    session.refresh(job.application)
    approval = outbound.get_approval(session, candidate)
    if approval is not None:
        session.refresh(approval)
    current = outbound.state(session, settings, candidate)
    if (candidate.revision != profile_revision or job.revision != job_revision
            or current.context_hash != context_hash or target_payload(job, job.application, candidate.profile) != targets):
        raise AnswerError(
            "Your profile, job, sharing choices or questions changed while the answers were being drafted. "
            "Review the current information and draft again. Nothing was saved.",
        )


def _store_run(session: Session, *, key: str, job: Job, candidate: Candidate, model: str, prompt_revision: str,
               context_hash: str, targets: list[dict], content: list[dict], usage: dict, attempts: int) -> AIRun:
    """Stage the cache row; draft_answers() commits it together with the drafts."""
    session.execute(delete(AIRun).where(AIRun.cache_key == key))
    run = AIRun(
        operation=DRAFT_ANSWERS, cache_key=key, job_id=job.id, model=model, prompt_revision=prompt_revision,
        input_revisions={"job": job.revision, "profile": candidate.revision, "outbound": context_hash,
                         "questions": [t["id"] for t in targets]},
        result={"answers": content}, usage=usage, attempts=attempts, created_at=utcnow(),
    )
    session.add(run)
    session.flush()
    return run


def draft_answers(session: Session, client: OpenRouterClient, settings: Settings, job: Job,
                  candidate: Candidate, force: bool = False) -> AIRun:
    """Draft answers to the job's open questions that still lack one, leaving acceptance to the candidate."""
    with operations.exclusive(DRAFT_ANSWERS, job.id):
        state = outbound.state(session, settings, candidate)
        context, context_hash = state.context, state.context_hash
        if context is None or context_hash is None:
            raise operations.ApprovalNeeded()
        application = job.application
        targets = target_payload(job, application, candidate.profile)
        if not targets:
            raise NothingToDo("Every open question already has an answer your profile supports, "
                              "or there are no open questions to draft.")
        profile_revision, job_revision = candidate.revision, job.revision
        prompt_revision = prompts.DRAFT_ANSWERS_REVISION
        job_data = job_context(job)
        inputs = {"context": context, "job": job_data, "questions": targets}
        key = operations.cache_key(DRAFT_ANSWERS, prompt_revision, client.model, inputs)

        target_by_id = {t["id"]: t for t in targets}
        shared = resume_service._shared_profile(context)
        shared_sources = profile_service.sources(shared)
        shared_ids = outbound.source_ids(context)
        extra = detection_terms(candidate.profile, job)

        def validate(arguments: dict) -> list[dict]:
            parsed = DraftedAnswers.model_validate(arguments)
            if not parsed.answers:
                raise ResultProblem(["No answers were returned. Answer the questions the candidate content supports."])
            problems, content, seen = [], [], set()
            for item in parsed.answers:
                qid = item.question_id
                target = target_by_id.get(qid)
                if target is None:
                    problems.append(f"{qid!r} is not one of the listed questions; use {sorted(target_by_id)}")
                    continue
                if qid in seen:
                    problems.append(f"{qid}: answered more than once")
                    continue
                seen.add(qid)
                text, cited = item.text.strip(), list(dict.fromkeys(item.sources))
                outside = [s for s in cited if s not in shared_ids]
                if outside:
                    problems.append(f"{qid}: sources not in the candidate content: {', '.join(outside)}")
                    continue
                found = answer_service.validate_answer(candidate.profile, job, text, cited, target["limit"], target["unit"])
                if not found:  # also check the exact (possibly reworded) content that was shared
                    found = [f"shared content: {p}" for p in answer_service.cited_claim_problems(
                        text, shared_sources, cited, extra,
                    )]
                problems.extend(f"{qid}: {p}" for p in found)
                content.append({"question_id": qid, "text": text, "sources": cited})
            if problems:
                raise ResultProblem(problems)
            return content

        def current() -> None:
            _assert_current(session, settings, job, candidate, profile_revision, job_revision, context_hash, targets)

        try:
            run = None if force else operations.find_run(session, key)
            if run is not None:
                try:
                    content = validate(run.result)
                except (ResultProblem, ValueError, KeyError) as exc:
                    problems = exc.problems if isinstance(exc, ResultProblem) else []
                    raise AIError("invalid", "The cached answers no longer pass validation. "
                                             "Draft again; your existing work is unchanged.",
                                  problems=problems) from None
                # A no-op write obtains the database writer lock before the final
                # check, so another session cannot change inputs mid-save.
                session.execute(update(AIRun).where(AIRun.id == run.id).values(cache_key=run.cache_key))
            else:
                result = client.structured(
                    DRAFT_ANSWERS, prompts.draft_answers_messages(context, job_data, targets),
                    prompts.DRAFT_ANSWERS_TOOL, validate, before_attempt=current,
                )
                current()
                content = result.value
                run = _store_run(
                    session, key=key, job=job, candidate=candidate, model=client.model,
                    prompt_revision=prompt_revision, context_hash=context_hash, targets=targets,
                    content=content, usage=result.usage, attempts=result.attempts,
                )
            # The writer lock is held now. Check again, then save drafts and run together.
            current()
            answer_service.replace_drafts(job.application, [
                AnswerDraft(
                    question_id=item["question_id"], text=item["text"], sources=item["sources"], model=run.model,
                    prompt_revision=run.prompt_revision, outbound_hash=context_hash,
                    profile_revision=profile_revision, job_revision=job_revision, created_at=run.created_at,
                )
                for item in content
            ])
            session.commit()
            return run
        except Exception:
            session.rollback()
            raise
