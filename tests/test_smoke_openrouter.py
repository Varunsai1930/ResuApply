"""Complete smoke coverage through the runtime client, using only an offline provider."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AIRun, Candidate, Job
from app.services import package
from scripts import fake_ai_server, smoke_openrouter
from tests.conftest import FAKE_KEY, make_settings, tool_response
from tests.synthetic import DEMO_REQUIREMENTS

TOOLS = ["return_requirements", "return_evidence_suggestions", "return_resume", "return_answers", "return_cover_letter"]


def capture_disposed_database(monkeypatch):
    """Read the disposable DB before its engine is disposed; never open settings.database_path."""
    original = smoke_openrouter.make_engine
    observed = {"paths": [], "states": []}

    def make_engine(url):
        engine = original(url)
        observed["paths"].append(Path(engine.url.database))
        dispose = engine.dispose

        def inspect_and_dispose():
            try:
                with Session(engine) as session:
                    job = session.scalars(select(Job)).first()
                    candidate = session.scalars(select(Candidate)).first()
                    observed["states"].append({
                        "operations": list(session.scalars(select(AIRun.operation).order_by(AIRun.id))),
                        "profile_revision": candidate.revision if candidate else None,
                        "answers": list(job.application.answers) if job else [],
                        "approval": job.application.approval if job else None,
                        "snapshots": list(job.application.submitted_snapshots) if job else [],
                        "review_state": package.review_state(job, job.application, candidate).value if job else None,
                    })
            finally:
                dispose()

        monkeypatch.setattr(engine, "dispose", inspect_and_dispose)
        return engine

    monkeypatch.setattr(smoke_openrouter, "make_engine", make_engine)
    return observed


def test_complete_smoke_runs_five_operations_accepts_and_freezes_only_disposable_data(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, openrouter_model="fake/local-stand-in")
    settings.resolved_data_dir.mkdir(parents=True)
    settings.database_path.write_bytes(b"existing user database must not be opened")
    observed = capture_disposed_database(monkeypatch)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        return fake_ai_server._handle(request)

    assert smoke_openrouter.main(settings, transport=httpx.MockTransport(provider)) == 0
    assert [request["tool_choice"]["function"]["name"] for request in requests] == TOOLS
    state = observed["states"][0]
    assert state["operations"] == ["parse_job", "suggest_evidence", "tailor_resume", "draft_answers",
                                   "draft_cover_letter"]
    assert state["profile_revision"] == 1 and state["review_state"] == "approved"
    assert len(state["answers"]) == 1 and state["answers"][0].origin == "ai_draft"
    assert state["answers"][0].sources and state["approval"] is not None
    assert len(state["snapshots"]) == 1
    assert state["snapshots"][0].answers[0]["text"] == state["answers"][0].text
    assert state["snapshots"][0].resume_html and state["snapshots"][0].cover_letter
    assert settings.database_path.read_bytes() == b"existing user database must not be opened"
    assert observed["paths"] and all(not path.parent.exists() for path in observed["paths"])
    printed = capsys.readouterr().out
    assert "4. draft_answers" in printed and "5. draft_cover_letter" in printed
    assert "submitted snapshot validated" in printed
    assert "Smoke test passed." in printed and FAKE_KEY not in printed
    sent = json.dumps(requests)
    for private in ("jordan@example.com", "+1 555 0100", "Jordan Example", "github.com/jordan-example"):
        assert private not in sent


def test_comparable_extraction_cannot_skip_evidence_stage(tmp_path, capsys):
    settings = make_settings(tmp_path, openrouter_model="fake/local-stand-in")
    requested = []

    def provider(request):
        body = json.loads(request.content)
        name = body["tool_choice"]["function"]["name"]
        requested.append(name)
        if name == "return_requirements":
            # This single skill criterion is already Met by the synthetic profile.
            return httpx.Response(200, json=tool_response(name, {"requirements": [copy.deepcopy(DEMO_REQUIREMENTS[0])]}))
        return fake_ai_server._handle(request)

    assert smoke_openrouter.main(settings, transport=httpx.MockTransport(provider)) == 0
    assert requested == TOOLS
    assert "Skipped:" not in capsys.readouterr().out


@pytest.mark.parametrize("failed_tool", TOOLS)
def test_provider_failure_exits_cleans_disposable_work_and_redacts_key(
    tmp_path, monkeypatch, capsys, failed_tool,
):
    settings = make_settings(tmp_path, openrouter_model="fake/local-stand-in")
    observed = capture_disposed_database(monkeypatch)
    requested = []

    def provider(request):
        name = json.loads(request.content)["tool_choice"]["function"]["name"]
        requested.append(name)
        if name == failed_tool:
            return httpx.Response(401, json={"error": {"message": "Rejected key " + FAKE_KEY}})
        return fake_ai_server._handle(request)

    assert smoke_openrouter.main(settings, transport=httpx.MockTransport(provider)) == 1
    assert requested == TOOLS[:TOOLS.index(failed_tool) + 1]
    assert observed["states"] and observed["states"][0]["approval"] is None
    assert not observed["states"][0]["snapshots"]
    assert all(not path.parent.exists() for path in observed["paths"])
    assert not settings.database_path.exists()
    printed = capsys.readouterr().out
    assert "FAILED (auth)" in printed and "Smoke test passed." not in printed
    assert FAKE_KEY not in printed and "[redacted]" in printed


def test_key_reflected_in_model_text_is_redacted_from_progress_output(tmp_path, capsys):
    settings = make_settings(tmp_path, openrouter_model="fake/local-stand-in")

    def provider(request):
        response = fake_ai_server._handle(request)
        body = response.json()
        name = json.loads(request.content)["tool_choice"]["function"]["name"]
        if name == "return_requirements":
            arguments = json.loads(body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
            arguments["requirements"][0]["text"] += " " + FAKE_KEY
            return httpx.Response(200, json=tool_response(name, arguments))
        return response

    assert smoke_openrouter.main(settings, transport=httpx.MockTransport(provider)) == 0
    printed = capsys.readouterr().out
    assert FAKE_KEY not in printed and "[redacted]" in printed


def test_repeated_invalid_answer_draft_fails_without_approving_or_freezing(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, openrouter_model="fake/local-stand-in")
    observed = capture_disposed_database(monkeypatch)
    requested = []

    def provider(request):
        name = json.loads(request.content)["tool_choice"]["function"]["name"]
        requested.append(name)
        response = fake_ai_server._handle(request)
        if name == "return_answers":
            body = response.json()
            arguments = json.loads(body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
            arguments["answers"][0]["text"] = "Built Java services for 10 million users"
            return httpx.Response(200, json=tool_response(name, arguments))
        return response

    assert smoke_openrouter.main(settings, transport=httpx.MockTransport(provider)) == 1
    assert requested == [*TOOLS[:4], "return_answers"]
    state = observed["states"][0]
    assert state["answers"] == [] and state["approval"] is None and state["snapshots"] == []
    assert "draft_answers" not in state["operations"]
    assert all(not path.parent.exists() for path in observed["paths"])
    printed = capsys.readouterr().out
    assert "FAILED (invalid)" in printed and "Smoke test passed." not in printed


def test_missing_key_exits_without_creating_client_database_or_network(tmp_path, monkeypatch, capsys):
    settings = Settings(data_dir=tmp_path / "unused", _env_file=None, openrouter_api_key=None)

    def forbidden(*args, **kwargs):
        raise AssertionError("missing-key smoke should do no work")

    monkeypatch.setattr(smoke_openrouter, "make_engine", forbidden)
    monkeypatch.setattr(smoke_openrouter, "OpenRouterClient", forbidden)
    assert smoke_openrouter.main(settings) == 2
    assert "nothing to test" in capsys.readouterr().out
    assert not settings.resolved_data_dir.exists()
