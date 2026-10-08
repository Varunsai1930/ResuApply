"""All smoke stages run locally, and a revoked sharing choice blocks retries."""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.orm import Session

from app.ai.client import OpenRouterClient
from app.ai.resume import tailor_resume
from app.models import AIRun, Candidate
from app.services import jobs, outbound, profile, resume
from scripts import smoke_openrouter
from scripts.fake_ai_server import _handle
from tests.conftest import make_settings


def test_all_three_smoke_stages_use_only_the_local_fake_provider(tmp_path, monkeypatch, capsys):
    settings = make_settings(tmp_path, openrouter_model="fake/local-stand-in")
    requests = []
    clients = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return _handle(request)

    def local_client(_key, model):
        client = OpenRouterClient("fake-local-key", model, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(smoke_openrouter, "get_settings", lambda: settings)
    monkeypatch.setattr(smoke_openrouter, "OpenRouterClient", local_client)
    try:
        assert smoke_openrouter.main() == 0
        assert [body["tool_choice"]["function"]["name"] for body in requests] == [
            "return_requirements", "return_evidence_suggestions", "return_resume",
        ]
        printed = capsys.readouterr().out
        assert "3. tailor_resume" in printed and "fictional resume was accepted locally" in printed
        assert "Smoke test passed." in printed and "fake-local-key" not in printed
        assert not settings.database_path.exists()  # smoke database is disposable
    finally:
        for client in clients:
            client.close()


def test_sharing_revoked_after_malformed_response_prevents_corrective_retry(session, sample_profile, tmp_path):
    settings = make_settings(tmp_path, openrouter_model="fake/local-stand-in")
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(title="Intern", company="Example", description="Python required."))
    state = outbound.state(session, settings, candidate)
    outbound.approve(session, candidate, state.base_hash, outbound.excludable_ids(state.base), {})
    original = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    accepted = resume.accept(session, job, candidate, resume.proposal_token(original))
    candidate_id = candidate.id
    requests = []

    def revoke_during_first_response(request):
        requests.append(json.loads(request.content))
        with Session(session.get_bind()) as other:
            outbound.withdraw(other, other.get(Candidate, candidate_id))
        return httpx.Response(200, json={"choices": [{"message": None}]})

    client = OpenRouterClient("fake-local-key", settings.openrouter_model,
                              transport=httpx.MockTransport(revoke_during_first_response))
    try:
        with pytest.raises(resume.ResumeError, match="sharing choices changed"):
            tailor_resume(session, client, settings, job, candidate)
    finally:
        client.close()
    assert len(requests) == 1
    session.refresh(job.application)
    assert job.application.current_proposal == original
    assert job.application.accepted_package == accepted
    assert session.query(AIRun).filter_by(operation="tailor_resume").count() == 0
