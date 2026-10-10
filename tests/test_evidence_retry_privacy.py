"""Evidence requests recheck sharing choices before retries and result persistence."""

import copy

import httpx
import pytest
from sqlalchemy.orm import Session

from app.ai import operations
from app.ai.client import AIError, OpenRouterClient
from app.models import AIRun, Candidate, Job
from app.services import checklist, jobs, outbound, profile
from app.services.requirements import set_requirements
from tests.conftest import DEMO_JOB, DEMO_REQUIREMENTS, TEST_MODEL, make_settings, tool_response


@pytest.mark.parametrize("change", ["withdraw", "exclude", "edit", "profile", "job", "evidence"])
@pytest.mark.parametrize("valid_response", [False, True], ids=["before-retry", "before-save"])
def test_changed_inputs_stop_evidence_retry_or_save(session, sample_profile, tmp_path, change, valid_response):
    settings = make_settings(tmp_path)
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    set_requirements(session, job, copy.deepcopy(DEMO_REQUIREMENTS))
    state = outbound.state(session, settings, candidate)
    outbound.approve(session, candidate, state.base_hash, outbound.excludable_ids(state.base), {})
    candidate_id, job_id = candidate.id, job.id
    before = (copy.deepcopy(job.evidence), copy.deepcopy(job.overrides))
    requests = []

    def handle(request):
        requests.append(request)
        with Session(session.get_bind()) as other:
            current = other.get(Candidate, candidate_id)
            current_job = other.get(Job, job_id)
            view = outbound.state(other, settings, current)
            included = outbound.excludable_ids(view.base)
            if change == "withdraw":
                outbound.withdraw(other, current)
            elif change == "exclude":
                outbound.approve(other, current, view.base_hash, included - {"exp-1-b1"}, {})
            elif change == "edit":
                outbound.approve(other, current, view.base_hash, included, {"summary": "Edited shared summary"})
            elif change == "profile":
                profile.save(other, current.profile.model_dump() | {"summary": "Updated profile"})
            elif change == "job":
                jobs.update(other, current_job, jobs.clean_input(**(DEMO_JOB | {"title": "Updated role"})))
            else:
                checklist.set_override(other, current_job, "r4", "met", "Confirmed separately")
        source = "exp-1-b1" if valid_response else "invented-source"
        return httpx.Response(200, json=tool_response("return_evidence_suggestions", {"suggestions": [
            {"requirement_id": "r4", "source_ids": [source], "reason": "REST API"},
        ]}))

    client = OpenRouterClient("synthetic-key", TEST_MODEL, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises((operations.ApprovalNeeded, AIError)):
            operations.suggest_evidence(session, client, settings, job, candidate)
        assert len(requests) == 1
        assert session.query(AIRun).count() == 0
        session.refresh(job)
        assert job.evidence == before[0]
        if change != "evidence":
            assert job.overrides == before[1]
        else:
            assert job.overrides["r4"].status == "met"
    finally:
        client.close()
