"""The local demo transport exercises the real resume pipeline without a provider key."""

import httpx

from app.ai.client import OpenRouterClient
from app.ai.resume import tailor_resume
from app.services import jobs, outbound, profile, resume
from scripts.fake_ai_server import _handle


def test_fake_model_tailors_only_the_approved_content_and_preserves_profile(session, settings, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(title="Intern", company="Example", description="Python required."))
    shared = outbound.state(session, settings, candidate)
    outbound.approve(session, candidate, shared.base_hash,
                     outbound.excludable_ids(shared.base) - {"exp-1-b2", "proj-1"}, {})
    before = candidate.profile.model_dump()
    client = OpenRouterClient("fake-local-key", "fake/local-stand-in", transport=httpx.MockTransport(_handle))
    try:
        record = tailor_resume(session, client, settings, job, candidate)
        assert record.resume.projects == []
        assert [bullet.sources for bullet in record.resume.experience[0].bullets] == [["exp-1-b1"], ["exp-1-b3"]]
        accepted = resume.accept(session, job, candidate, resume.proposal_token(record))
        assert accepted.resume == record
        assert candidate.profile.model_dump() == before
    finally:
        client.close()
