"""A corrective retry must not resend content after the user changes its permission."""

import pytest
import httpx
from sqlalchemy.orm import Session

from app.ai.client import OpenRouterClient
from app.ai.resume import tailor_resume
from app.models import AIRun, Candidate, Job
from app.services import jobs, outbound, profile, resume
from tests.conftest import TEST_MODEL, make_settings, tool_response


@pytest.mark.parametrize("change", ["exclude", "edit", "withdraw", "profile", "job"])
def test_changes_after_invalid_response_block_retry_and_preserve_work(session, sample_profile, fake_ai, tmp_path, change):
    settings = make_settings(tmp_path)
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(title="Intern", company="Example", description="Python required."))
    shared = outbound.state(session, settings, candidate)
    outbound.approve(session, candidate, shared.base_hash, outbound.excludable_ids(shared.base), {})
    existing = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    accepted = resume.accept(session, job, candidate, resume.proposal_token(existing))
    candidate_id, job_id = candidate.id, job.id
    invalid = {"experience": [{"entry": "exp-1", "bullets": [{"text": "Built Java services", "sources": ["exp-1-b1"]}]}]}
    fake_ai.push(tool_response("return_resume", invalid), tool_response("return_resume", invalid))

    def handle(request):
        response = fake_ai._handle(request)
        if len(fake_ai.requests) == 1:
            with Session(session.get_bind()) as other:
                current = other.get(Candidate, candidate_id)
                state = outbound.state(other, settings, current)
                included = outbound.excludable_ids(state.base)
                if change == "exclude":
                    outbound.approve(other, current, state.base_hash, included - {"proj-1"}, {})
                elif change == "edit":
                    outbound.approve(other, current, state.base_hash, included, {"summary": "New shared summary"})
                elif change == "withdraw":
                    outbound.withdraw(other, current)
                elif change == "profile":
                    edited = current.profile.model_dump()
                    edited["summary"] = "Updated candidate facts"
                    profile.save(other, edited)
                else:
                    current_job = other.get(Job, job_id)
                    jobs.update(other, current_job, jobs.clean_input(title="Updated role", company="Example", description="Python required."))
        return response

    client = OpenRouterClient("synthetic-key", TEST_MODEL, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(resume.ResumeError, match="changed while"):
            tailor_resume(session, client, settings, job, candidate)
        assert len(fake_ai.requests) == 1, "No second request may resend the original context"
        session.refresh(job.application)
        assert job.application.current_proposal == existing
        assert job.application.accepted_package == accepted
        assert session.query(AIRun).count() == 0
    finally:
        client.close()
