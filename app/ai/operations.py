"""The AI operations of Milestone 2 and the rules their results must pass.

- ``extract_requirements`` (parse_job): job text in, proposed requirements out. Every excerpt
  must be found in the description; the user reviews and corrects the proposal before saving.
- ``suggest_evidence``: the approved reduced candidate context and the requirements that are
  still Unknown in, suggested profile sources out. Every cited ID must exist in what was sent.
  Suggestions are shown separately and change nothing until the user accepts one.

Only validated results are stored (``ai_runs``) and reused while the task inputs, model and
prompt revision are unchanged. A failed request never touches saved requirements or evidence.
Only one request per operation and job runs at a time.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import AIRun, Candidate, Job
from ..services import checklist, outbound
from ..services import profile as profile_service
from ..services.profile import FieldError
from ..services.requirements import RequirementsInvalid, validate_requirements
from ..services.text import canonical_json
from . import prompts
from .client import AIError, OpenRouterClient, ResultProblem

PARSE_JOB = "parse_job"
SUGGEST_EVIDENCE = "suggest_evidence"


class ApprovalNeeded(Exception):
    """Candidate content can't be sent until the user approves the outbound preview."""


class NothingToDo(Exception):
    pass


# ---------------------------------------------------------------- shared plumbing

_locks: dict[tuple[str, int], threading.Lock] = {}
_locks_guard = threading.Lock()


@contextmanager
def exclusive(operation: str, job_id: int) -> Iterator[None]:
    """Block a duplicate request while the same operation runs for the same job."""
    with _locks_guard:
        lock = _locks.setdefault((operation, job_id), threading.Lock())
    if not lock.acquire(blocking=False):
        raise AIError("busy", "This request is already running. Wait for it to finish.")
    try:
        yield
    finally:
        lock.release()


def cache_key(operation: str, prompt_revision: str, model: str, inputs: dict) -> str:
    payload = canonical_json({"op": operation, "prompt": prompt_revision, "model": model, "inputs": inputs})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_run(session: Session, key: str) -> AIRun | None:
    return session.scalars(select(AIRun).where(AIRun.cache_key == key)).first()


def latest_run(session: Session, operation: str, job_id: int) -> AIRun | None:
    return session.scalars(
        select(AIRun).where(AIRun.operation == operation, AIRun.job_id == job_id)
        .order_by(AIRun.created_at.desc(), AIRun.id.desc()).limit(1)
    ).first()


def _store(session: Session, *, operation: str, key: str, job: Job, model: str, prompt_revision: str,
           input_revisions: dict, result: dict, usage: dict, attempts: int) -> AIRun:
    session.execute(delete(AIRun).where(AIRun.cache_key == key))
    run = AIRun(operation=operation, cache_key=key, job_id=job.id, model=model, prompt_revision=prompt_revision,
                input_revisions=input_revisions, result=result, usage=usage, attempts=attempts)
    session.add(run)
    session.commit()
    return run


# ---------------------------------------------------------------- parse_job

class _DraftRequirement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    category: str = "other"
    importance: str = "unspecified"
    excerpt: str
    criterion: dict | None = None


class ParsedJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    requirements: list[_DraftRequirement] = Field(max_length=60)


def _draft_items(parsed: ParsedJob) -> list[dict]:
    items = []
    for req in parsed.requirements:
        crit = {k: v for k, v in (req.criterion or {}).items() if v is not None} or None
        items.append({"text": req.text, "category": req.category, "importance": req.importance,
                      "excerpt": req.excerpt, "criterion": crit})
    return items


def job_inputs(job: Job) -> dict:
    """Everything parse_job sends: the job's own details. No candidate data."""
    return {"title": job.title, "company": job.company, "location": job.location, "description": job.description}


def parse_job_key(job: Job, model: str) -> str:
    return cache_key(PARSE_JOB, prompts.PARSE_JOB_REVISION, model, job_inputs(job))


@dataclass
class ExtractionOutcome:
    items: list[dict]  # proposed requirements, without IDs
    problems: list[FieldError] = field(default_factory=list)  # set when the user must correct the proposal
    run: AIRun | None = None
    from_cache: bool = False


def extract_requirements(session: Session, client: OpenRouterClient, job: Job, force: bool = False) -> ExtractionOutcome:
    key = parse_job_key(job, client.model)
    if not force and (run := find_run(session, key)):
        return ExtractionOutcome(run.result["requirements"], run=run, from_cache=True)

    def validate(arguments: dict) -> list[dict]:
        items = _draft_items(ParsedJob.model_validate(arguments))
        if not items:
            raise ResultProblem(["No requirements were returned."])
        try:
            validate_requirements(job, items)
        except RequirementsInvalid as exc:
            raise ResultProblem([e.message for e in exc.errors]) from None
        return items

    with exclusive(PARSE_JOB, job.id):
        try:
            result = client.structured(PARSE_JOB, prompts.parse_job_messages(job_inputs(job)), prompts.PARSE_JOB_TOOL, validate)
        except AIError as exc:
            # A well-formed proposal that still breaks a rule goes back to the user for correction.
            if exc.kind != "invalid" or exc.last_arguments is None:
                raise
            try:
                items = _draft_items(ParsedJob.model_validate(exc.last_arguments))
            except ValueError:
                raise exc from None
            if not items:
                raise
            items = [item | {"_form": f"req-{i}"} for i, item in enumerate(items)]
            try:
                validate_requirements(job, items)
                problems: list[FieldError] = []
            except RequirementsInvalid as invalid:
                problems = invalid.errors
            return ExtractionOutcome(items, problems=problems)
    run = _store(session, operation=PARSE_JOB, key=key, job=job, model=client.model,
                 prompt_revision=prompts.PARSE_JOB_REVISION, input_revisions={"job": job.revision},
                 result={"requirements": result.value}, usage=result.usage, attempts=result.attempts)
    return ExtractionOutcome(result.value, run=run)


def cached_proposal(session: Session, job: Job, model: str) -> AIRun | None:
    """The validated proposal for the job as it is now, if one was made with this model."""
    return find_run(session, parse_job_key(job, model))


# ---------------------------------------------------------------- suggest_evidence

class _Suggestion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    requirement_id: str
    source_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class EvidenceSuggestions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    suggestions: list[_Suggestion]


def evidence_targets(job: Job, candidate: Candidate) -> list[dict]:
    """Requirements that are still Unknown, not overridden and still quoted in the description."""
    results = checklist.evaluate(job, candidate.profile)
    return [
        {"id": r.id, "text": r.text, "category": r.category, "excerpt": r.excerpt}
        for r in results
        if r.computed_status == checklist.UNKNOWN and r.override is None and r.excerpt_found
    ]


def suggest_evidence(session: Session, client: OpenRouterClient, settings: Settings, job: Job,
                     candidate: Candidate, force: bool = False) -> AIRun:
    state = outbound.state(session, settings, candidate)
    context = state.context
    if context is None:
        raise ApprovalNeeded()
    targets = evidence_targets(job, candidate)
    if not targets:
        raise NothingToDo("Every requirement is already Met, Unmet or overridden, so there is nothing to suggest.")
    inputs = {"context": context, "requirements": targets}
    key = cache_key(SUGGEST_EVIDENCE, prompts.SUGGEST_EVIDENCE_REVISION, client.model, inputs)
    if not force and (run := find_run(session, key)):
        return run

    allowed = outbound.source_ids(context)
    target_ids = {t["id"] for t in targets}

    def validate(arguments: dict) -> list[dict]:
        parsed = EvidenceSuggestions.model_validate(arguments)
        problems = []
        merged: dict[str, dict] = {}
        for s in parsed.suggestions:
            if s.requirement_id not in target_ids:
                problems.append(f"Unknown requirement_id {s.requirement_id!r}; use one of {sorted(target_ids)}")
                continue
            unknown = [i for i in s.source_ids if i not in allowed]
            if unknown:
                problems.append(f"{s.requirement_id}: source IDs not in the candidate content: {', '.join(unknown)}")
                continue
            entry = merged.setdefault(s.requirement_id, {"requirement_id": s.requirement_id, "source_ids": [], "reason": ""})
            entry["source_ids"] = list(dict.fromkeys([*entry["source_ids"], *s.source_ids]))
            entry["reason"] = " ".join(x for x in (entry["reason"], s.reason.strip()[:300]) if x)
        if problems:
            raise ResultProblem(problems)
        return [s for s in merged.values() if s["source_ids"]]

    with exclusive(SUGGEST_EVIDENCE, job.id):
        result = client.structured(
            SUGGEST_EVIDENCE, prompts.suggest_evidence_messages(context, targets), prompts.SUGGEST_EVIDENCE_TOOL, validate,
        )
    return _store(session, operation=SUGGEST_EVIDENCE, key=key, job=job, model=client.model,
                  prompt_revision=prompts.SUGGEST_EVIDENCE_REVISION,
                  input_revisions={"job": job.revision, "profile": candidate.revision, "outbound": state.context_hash},
                  result={"suggestions": result.value}, usage=result.usage, attempts=result.attempts)


@dataclass(frozen=True)
class SuggestedSource:
    id: str
    text: str
    reason: str


@dataclass
class SuggestionView:
    by_requirement: dict[str, list[SuggestedSource]]
    run: AIRun | None
    out_of_date: bool  # the latest suggestions were made for an older job or profile revision


def current_suggestions(session: Session, job: Job, candidate: Candidate | None) -> SuggestionView:
    """Suggestions still waiting for a decision, from the latest run made for the current job and profile."""
    run = latest_run(session, SUGGEST_EVIDENCE, job.id)
    if run is None or candidate is None:
        return SuggestionView({}, run, False)
    revisions = run.input_revisions
    if revisions.get("job") != job.revision or revisions.get("profile") != candidate.revision:
        return SuggestionView({}, run, True)
    sources = profile_service.sources(candidate.profile)
    view: dict[str, list[SuggestedSource]] = {}
    for item in run.result.get("suggestions", []):
        link = job.evidence.get(item["requirement_id"])
        decided = set(link.sources + link.rejected) if link else set()
        if job.overrides.get(item["requirement_id"]):
            continue
        pending = [
            SuggestedSource(i, sources[i].text, item.get("reason", ""))
            for i in item["source_ids"] if i in sources and i not in decided
        ]
        if pending:
            view[item["requirement_id"]] = pending
    return SuggestionView(view, run, False)
