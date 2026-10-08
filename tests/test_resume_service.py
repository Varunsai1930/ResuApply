"""Proposal/accepted isolation, persistence and stale-input concurrency guards."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy.orm import Session

from app.schemas.tracking import ReviewState, TrackingStatus
from app.services import jobs, profile, resume
from tests.conftest import DEMO_JOB, SAMPLE_PROFILE


def seed(session):
    profile.save(session, copy.deepcopy(SAMPLE_PROFILE))
    return jobs.create(session, jobs.clean_input(**DEMO_JOB)), profile.get_candidate(session)


def proposal(session, job, candidate):
    return resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))


def test_profile_draft_quotes_sources_and_renderer_uses_local_metadata(session):
    job, candidate = seed(session)
    record = proposal(session, job, candidate)
    rendered = resume.render_context(candidate.profile, record.resume)
    assert rendered["contact"]["name"] == "Jordan Example"
    assert rendered["experience"][0]["organization"] == "Sample Analytics"
    bullet = rendered["experience"][0]["bullets"][0]
    assert bullet["text"] == candidate.profile.experience[0].bullets[0].text
    assert bullet["originals"][0] == {"id": "exp-1-b1", "text": bullet["text"]}
    assert rendered["projects"][0]["link"] == candidate.profile.projects[0].link
    assert rendered["education"][0]["bullets"][0]["text"] == candidate.profile.education[0].bullets[0].text


def test_selected_custom_skills_use_canonical_profile_spelling(session):
    job, candidate = seed(session)
    edited = candidate.profile.model_dump()
    edited["skills"].append({"name": "Custom Skill", "category": "Tools"})
    profile.save(session, edited)
    record = resume.propose(session, job, candidate, {"skills": ["custom skill"]})
    assert record.resume.skills == ["Custom Skill"]
    assert resume.render_context(candidate.profile, record.resume)["skills"] == ["Custom Skill"]


def test_valid_long_profile_bullets_and_large_lists_can_be_prepared(session):
    job, candidate = seed(session)
    edited = candidate.profile.model_dump()
    long_text = "Wrote unit tests for the billing module. " * 325
    edited["experience"][0]["bullets"] = [{"text": long_text}] + [
        {"text": f"Verified billing scenario {i}"} for i in range(201)
    ]
    edited["skills"] += [{"name": f"Custom Skill {i}"} for i in range(501)]
    profile.save(session, edited)
    record = proposal(session, job, candidate)
    assert record.resume.experience[0].bullets[0].text == long_text.strip()
    assert len(record.resume.experience[0].bullets) == 202
    assert len(record.resume.skills) == 506
    package = resume.accept(session, job, candidate, resume.proposal_token(record))
    assert package.resume == record


def test_proposal_does_not_accept_or_change_tracking_and_accept_persists(session):
    job, candidate = seed(session)
    record = proposal(session, job, candidate)
    assert job.application.current_proposal == record and job.application.accepted_package is None
    package = resume.accept(session, job, candidate, resume.proposal_token(record))
    assert package.resume == record and package.accepted_at is not None
    assert job.application.review == ReviewState.DRAFT
    assert job.application.status == TrackingStatus.SAVED
    with Session(session.bind) as reopened:
        loaded = jobs.get(reopened, job.id)
        assert loaded.application.accepted_package == package
        assert resume.state(loaded, profile.get_candidate(reopened)).accepted == record


def test_regeneration_preserves_accepted_resume_and_failed_generation_preserves_both(session):
    job, candidate = seed(session)
    first = proposal(session, job, candidate)
    package = resume.accept(session, job, candidate, resume.proposal_token(first))
    second = resume.propose(session, job, candidate, {"experience": []})
    assert second != first and job.application.accepted_package == package
    with pytest.raises(resume.ResumeInvalid):
        resume.propose(session, job, candidate, {"skills": ["Java"]})
    session.refresh(job.application)
    assert job.application.current_proposal == second and job.application.accepted_package == package


def test_accept_requires_the_reviewed_token_and_preserves_accepted_record(session):
    job, candidate = seed(session)
    first = proposal(session, job, candidate)
    package = resume.accept(session, job, candidate, resume.proposal_token(first))
    second = proposal(session, job, candidate)
    with pytest.raises(resume.ResumeError, match="proposal changed"):
        resume.accept(session, job, candidate, resume.proposal_token(first))
    assert second != first and job.application.accepted_package == package


@pytest.mark.parametrize("change", ["profile", "job"])
def test_changed_inputs_make_proposal_and_accepted_stale(session, change):
    job, candidate = seed(session)
    first = proposal(session, job, candidate)
    package = resume.accept(session, job, candidate, resume.proposal_token(first))
    if change == "profile":
        edited = candidate.profile.model_dump()
        edited["contact"]["phone"] = "555-0199"
        profile.save(session, edited)
    else:
        jobs.update(session, job, jobs.clean_input(**(DEMO_JOB | {"title": "Changed title"})))
    current = resume.state(job, candidate)
    assert current.proposal_stale and current.accepted_stale
    with pytest.raises(resume.ResumeError, match="profile or job changed"):
        resume.accept(session, job, candidate, resume.proposal_token(first))
    assert job.application.accepted_package == package


def test_propose_rejects_explicit_older_input_revisions(session):
    job, candidate = seed(session)
    first = proposal(session, job, candidate)
    with pytest.raises(resume.ResumeError):
        resume.propose(session, job, candidate, first.resume, expected_profile_revision=candidate.revision - 1)
    assert job.application.current_proposal == first


@pytest.mark.parametrize("change", ["profile", "job", "proposal"])
def test_accept_atomically_checks_database_inputs_despite_stale_session(session, change):
    job, candidate = seed(session)
    first = proposal(session, job, candidate)
    with Session(session.bind) as other:
        other_job, other_candidate = jobs.get(other, job.id), profile.get_candidate(other)
        if change == "profile":
            edited = other_candidate.profile.model_dump()
            edited["contact"]["phone"] = "555-0199"
            profile.save(other, edited)
        elif change == "job":
            jobs.update(other, other_job, jobs.clean_input(**(DEMO_JOB | {"title": "Changed title"})))
        else:
            proposal(other, other_job, other_candidate)
    with pytest.raises(resume.ResumeError, match="changed since you reviewed"):
        resume.accept(session, job, candidate, resume.proposal_token(first))
    session.refresh(job.application)
    assert job.application.accepted_package is None


@pytest.mark.parametrize("change", ["profile", "job"])
def test_propose_atomically_rejects_old_inputs_despite_stale_session(session, change):
    job, candidate = seed(session)
    first = proposal(session, job, candidate)
    with Session(session.bind) as other:
        other_job, other_candidate = jobs.get(other, job.id), profile.get_candidate(other)
        if change == "profile":
            edited = other_candidate.profile.model_dump()
            edited["contact"]["phone"] = "555-0199"
            profile.save(other, edited)
        else:
            jobs.update(other, other_job, jobs.clean_input(**(DEMO_JOB | {"title": "Changed title"})))
    with pytest.raises(resume.ResumeError, match="changed while the resume"):
        proposal(session, job, candidate)
    session.refresh(job.application)
    assert job.application.current_proposal == first


def test_missing_profile_or_deleted_cited_source_marks_records_stale(session):
    job, candidate = seed(session)
    record = proposal(session, job, candidate)
    assert resume.state(job, None).proposal_stale
    broken = candidate.profile.model_copy(update={"experience": []})
    candidate.profile = broken
    assert resume.state(job, candidate).proposal_stale
    assert record.resume.experience
