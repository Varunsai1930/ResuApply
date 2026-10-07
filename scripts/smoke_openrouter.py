"""Live smoke test against OpenRouter with synthetic, non-identifying content.

Run it after adding OPENROUTER_API_KEY to .env:

    .venv/bin/python -m scripts.smoke_openrouter

It uses a throwaway database in a temporary folder (your data/ is untouched), sends a
fictional job and a fictional candidate to the configured model, and checks that both AI
operations return results that pass local validation. The key is never printed.
"""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

from app.ai import operations
from app.ai.client import AIError, OpenRouterClient
from app.config import get_settings
from app.db import init_db, make_engine, make_session_factory
from app.services import checklist, outbound
from app.services import jobs as job_service
from app.services import profile as profile_service
from app.services.requirements import RequirementsInvalid, set_requirements
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE


def main() -> int:
    settings = get_settings()
    if not settings.ai_configured:
        print("OPENROUTER_API_KEY is not set in .env; nothing to test.")
        return 2
    print(f"Model: {settings.openrouter_model} ({settings.openrouter_model_trust})")
    client = OpenRouterClient(settings.openrouter_api_key.get_secret_value(), settings.openrouter_model)
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(f"sqlite:///{Path(tmp) / 'smoke.db'}")
        init_db(engine)
        with make_session_factory(engine)() as session:
            candidate = profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate
            job = job_service.create(session, job_service.clean_input(**DEMO_JOB))

            print("\n1. parse_job (sends the fictional posting, including a prompt-injection line)")
            try:
                outcome = operations.extract_requirements(session, client, job)
            except AIError as exc:
                print(f"   FAILED ({exc.kind}): {exc.message}")
                return 1
            if outcome.problems:
                print("   The proposal needs correction:")
                for problem in outcome.problems:
                    print(f"   - {problem.message}")
                return 1
            print(f"   {len(outcome.items)} requirements, {outcome.run.attempts} attempt(s), usage {outcome.run.usage}")
            for item in outcome.items:
                crit = (item.get("criterion") or {}).get("type", "-")
                print(f"   - [{item['importance']}] {item['text']}  (criterion: {crit})")
            if any("10 years" in item["text"] for item in outcome.items):
                print("   NOTE: the model turned the injected line into a requirement; review would catch it.")
            try:
                set_requirements(session, job, outcome.items)
            except RequirementsInvalid as exc:
                print(f"   FAILED to save: {[e.message for e in exc.errors]}")
                return 1
            if profile_service.get_candidate(session).revision != 1:
                print("   FAILED: the profile changed")
                return 1

            print("\n2. suggest_evidence (sends the fictional candidate's reduced context)")
            state = outbound.state(session, settings, candidate)
            outbound.approve(session, candidate, state.base_hash, outbound.excludable_ids(state.base), {})
            try:
                run = operations.suggest_evidence(session, client, settings, job, candidate)
            except operations.NothingToDo as exc:
                print(f"   Skipped: {exc}")
                return 0
            except AIError as exc:
                print(f"   FAILED ({exc.kind}): {exc.message}")
                return 1
            print(f"   {run.attempts} attempt(s), usage {run.usage}")
            for item in run.result["suggestions"]:
                print(f"   - {item['requirement_id']}: {', '.join(item['source_ids'])}  ({item['reason']})")
            counts = checklist.summary(checklist.evaluate(job, candidate.profile))
            print(f"\nChecklist (before accepting suggestions): {counts}")
        engine.dispose()
    client.close()
    print("\nSmoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
