"""Live smoke test against OpenRouter with synthetic, non-identifying content.

Run it after adding OPENROUTER_API_KEY to .env:

    .venv/bin/python -m scripts.smoke_openrouter

It uses a throwaway database in a temporary folder (your data/ is untouched), sends a
fictional job and a fictional candidate to the configured model, and checks that all AI
operations return results that pass local validation. It then accepts the fictional
resume and answer, approves their package, and freezes a local submitted snapshot.
The key is never printed. Nothing is submitted to an employer.
"""

from __future__ import annotations

import copy
import sys
import tempfile
from datetime import date
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from app.ai import operations
from app.ai import answers as answers_ai
from app.ai import resume as resume_ai
from app.ai.client import AIError, OpenRouterClient
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_session_factory
from app.services import checklist, outbound, package
from app.services import answers as answer_service
from app.services import jobs as job_service
from app.services import profile as profile_service
from app.services import resume as resume_service
from app.services.requirements import RequirementsInvalid, set_requirements
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE


PROBE_EXCERPT = "Provide an example of a backend project."
PROBE_REQUIREMENT = {"text": "Provide an example of a backend project", "category": "other",
                     "importance": "unspecified", "excerpt": PROBE_EXCERPT, "criterion": None}


def _exercise(session: Session, client: OpenRouterClient, settings: Settings, *, api_key: str) -> int:
    def report(message: str) -> None:
        print(message.replace(api_key, "[redacted]"))

    candidate = profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate
    original_profile = candidate.profile.model_dump(mode="json")
    job_input = DEMO_JOB | {"description": DEMO_JOB["description"] + "\n- " + PROBE_EXCERPT}
    job = job_service.create(session, job_service.clean_input(**job_input))

    report("\n1. parse_job (sends the fictional posting, including a prompt-injection line)")
    outcome = operations.extract_requirements(session, client, job)
    if outcome.problems:
        report("   The proposal needs correction:")
        for problem in outcome.problems:
            report(f"   - {problem.message}")
        return 1
    report(f"   {len(outcome.items)} requirements, {outcome.run.attempts} attempt(s), usage {outcome.run.usage}")
    for item in outcome.items:
        crit = (item.get("criterion") or {}).get("type", "-")
        report(f"   - [{item['importance']}] {item['text']}  (criterion: {crit})")
    if any("10 years" in item["text"] for item in outcome.items):
        report("   NOTE: the model turned the injected line into a requirement; review would catch it.")
    # The fixed, excerpt-backed review item always remains Unknown without linked
    # evidence. It makes suggestion coverage independent of the model's choice of
    # comparable criteria, so NothingToDo cannot silently count as a passed stage.
    reviewed = [item for item in outcome.items if item["excerpt"] != PROBE_EXCERPT][:59]
    set_requirements(session, job, [*reviewed, copy.deepcopy(PROBE_REQUIREMENT)])
    report("   Added a fixed review item to exercise evidence suggestions.")

    report("\n2. suggest_evidence (sends the fictional candidate's reduced context)")
    state = outbound.state(session, settings, candidate)
    outbound.approve(session, candidate, state.base_hash, outbound.excludable_ids(state.base), {})
    run = operations.suggest_evidence(session, client, settings, job, candidate)
    report(f"   {run.attempts} attempt(s), usage {run.usage}")
    for item in run.result["suggestions"]:
        report(f"   - {item['requirement_id']}: {', '.join(item['source_ids'])}  ({item['reason']})")
    counts = checklist.summary(checklist.evaluate(job, candidate.profile))
    report(f"\nChecklist (before accepting suggestions): {counts}")

    report("\n3. tailor_resume (source-backed proposal, using the approved fictional context)")
    record = resume_ai.tailor_resume(session, client, settings, job, candidate)
    accepted = resume_service.accept(session, job, candidate, resume_service.proposal_token(record))
    report(f"   {len(record.resume.experience)} experience entries, {len(record.resume.projects)} projects")
    report("   Claims validated; the fictional resume was accepted locally.")
    accepted_resume = accepted.model_dump(mode="json")

    report("\n4. draft_answers (one fictional open-ended question, with sources and a length limit)")
    question = answer_service.add_question(session, job, "Describe a backend project you are proud of", limit=1000)
    run = answers_ai.draft_answers(session, client, settings, job, candidate)
    application = job.application
    draft = next((item for item in application.answer_drafts if item.question_id == question.id), None)
    if draft is None or application.answers:
        report("   FAILED: answer drafting did not create the expected review-only proposal.")
        return 1
    answer = answer_service.accept_draft(session, job, application, question.id, candidate,
                                         answer_service.draft_token(job, question, draft))
    if answer.origin != "ai_draft" or not answer.sources:
        report("   FAILED: the accepted answer lost its AI origin or source references.")
        return 1
    report(f"   {run.attempts} attempt(s), usage {run.usage}; fictional answer validated and accepted locally.")
    if application.accepted_package.model_dump(mode="json") != accepted_resume:
        report("   FAILED: drafting or accepting an answer changed the accepted resume.")
        return 1

    token = package.package_token(job, application, candidate)
    package.approve(session, job, candidate, token)
    snapshot = package.record_applied(session, job, candidate, token, on=date(2030, 1, 15), today=date(2030, 1, 15))
    if len(snapshot.answers) != 1 or snapshot.answers[0]["text"] != answer.text or not snapshot.resume_html:
        report("   FAILED: the fictional submitted snapshot did not retain the accepted resume and answer.")
        return 1
    session.refresh(candidate)
    if candidate.profile.model_dump(mode="json") != original_profile or candidate.revision != 1:
        report("   FAILED: an AI operation changed the fictional profile.")
        return 1
    report("   Fictional package approval and submitted snapshot validated in the temporary database.")
    return 0


def main(settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Run against the configured provider, or an explicitly supplied offline test transport."""
    settings = settings or get_settings()
    if not settings.ai_configured:
        print("OPENROUTER_API_KEY is not set in .env; nothing to test.")
        return 2
    key = settings.openrouter_api_key.get_secret_value().strip()
    print(f"Model: {settings.openrouter_model.replace(key, '[redacted]')} ({settings.openrouter_model_trust})")
    client = (OpenRouterClient(key, settings.openrouter_model) if transport is None else
              OpenRouterClient(key, settings.openrouter_model, transport=transport))
    try:
        with tempfile.TemporaryDirectory(prefix="resuapply-smoke-") as tmp:
            engine = make_engine(f"sqlite:///{Path(tmp) / 'smoke.db'}")
            try:
                init_db(engine)
                with make_session_factory(engine)() as session:
                    try:
                        result = _exercise(session, client, settings, api_key=key)
                    except (AIError, operations.ApprovalNeeded, operations.NothingToDo, RequirementsInvalid,
                            resume_service.ResumeError, answer_service.AnswerError, package.PackageError) as exc:
                        detail = str(exc).replace(key, "[redacted]")
                        print(f"   FAILED ({getattr(exc, 'kind', type(exc).__name__)}): {detail}")
                        result = 1
            finally:
                engine.dispose()
    finally:
        client.close()
    if result:
        return result
    print("\nSmoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
