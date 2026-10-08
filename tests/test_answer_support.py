"""An accepted AI-drafted answer must still be supported by the current profile at approval time."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from app.schemas.package import AnswerDraft
from app.services import answers as answer_service
from app.services import jobs as job_service
from app.services import package
from app.services import profile as profile_service
from app.services import resume as resume_service
from tests.conftest import SAMPLE_JOB, SAMPLE_PROFILE


@pytest.fixture
def prepared(session):
    """A profile, a job with an accepted resume and one open question answered by an accepted AI draft."""
    candidate = profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate
    job = job_service.create(session, job_service.clean_input(**SAMPLE_JOB))
    app = job.application
    resume_service.propose(session, job, candidate, resume_service.profile_draft(candidate.profile))
    resume_service.accept(session, job, candidate, resume_service.proposal_token(app.current_proposal))
    question = answer_service.add_question(session, job, "Why do you want this role?")
    draft = AnswerDraft(
        question_id=question.id, sources=["exp-1-b1"], model="test/model", prompt_revision="draft_answers/1",
        text="I built a Flask REST API in Python that served reporting data to 1,200 internal users.",
        profile_revision=candidate.revision, job_revision=job.revision, created_at=datetime.now(timezone.utc),
    )
    answer_service.replace_drafts(app, [draft])
    session.commit()
    answer_service.accept_draft(session, job, app, question.id, candidate.profile)
    return candidate, job, question


def _edit_bullet(session, candidate, text):
    edited = candidate.profile.model_dump()
    edited["experience"][0]["bullets"][0]["text"] = text
    return profile_service.save(session, edited).candidate


def _accept_fresh_resume(session, job, candidate):
    resume_service.propose(session, job, candidate, resume_service.profile_draft(candidate.profile))
    resume_service.accept(session, job, candidate, resume_service.proposal_token(job.application.current_proposal))


def test_supported_answer_approves(session, prepared):
    candidate, job, _ = prepared
    assert package.check(job, job.application, candidate)[0] == []
    package.approve(session, job, candidate)


def test_edited_source_blocks_reapproval_until_the_answer_is_fixed(session, prepared):
    candidate, job, question = prepared
    candidate = _edit_bullet(session, candidate, "Built an internal reporting API")
    _accept_fresh_resume(session, job, candidate)
    blockers, _ = package.check(job, job.application, candidate)
    assert len(blockers) == 1 and "no longer supported by your profile" in blockers[0]
    assert "1200" in blockers[0]  # the metric its source no longer states
    with pytest.raises(package.PackageError):
        package.approve(session, job, candidate)

    answer_service.set_answer(session, job, job.application, question.id, "I enjoy building internal tools.")
    assert package.check(job, job.application, candidate)[0] == []
    package.approve(session, job, candidate)


def test_removed_source_blocks_and_user_answers_are_not_rechecked(session, prepared):
    candidate, job, question = prepared
    edited = candidate.profile.model_dump()
    edited["experience"][0]["bullets"].pop(0)
    candidate = profile_service.save(session, edited).candidate
    _accept_fresh_resume(session, job, candidate)
    answer = next(a for a in job.application.answers if a.question_id == question.id)
    assert "unknown sources exp-1-b1" in " ".join(answer_service.answer_problems(job, question, answer, candidate.profile))
    # The user's own words are theirs; only AI-drafted answers are re-checked.
    answer_service.set_answer(session, job, job.application, question.id, "I built a reporting API for 1,200 users.")
    assert package.check(job, job.application, candidate)[0] == []


def test_workspace_flags_the_unsupported_answer(client, session, prepared):
    candidate, job, _ = prepared
    _edit_bullet(session, candidate, "Built an internal reporting API")
    page = client.get(f"/jobs/{job.id}").text
    assert "no longer supports this AI-drafted answer" in page
