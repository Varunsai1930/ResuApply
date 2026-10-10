"""Previously approved answers must pass current validation before being submitted."""

import pytest

from app.db import utcnow
from app.schemas.package import Answer, Approval
from app.schemas.tracking import ReviewState
from app.services import answers, jobs, package, profile, resume
from app.services.text import content_hash
from tests.synthetic import DEMO_JOB


def test_old_approval_cannot_submit_a_claim_supported_only_by_uncited_summary(session, sample_profile):
    sample_profile["summary"] = "Previously supported 5,000 users on legacy services."
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    record = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    resume.accept(session, job, candidate, resume.proposal_token(record))
    question = answers.add_question(session, job, "Describe a project")
    application = job.application

    # This was accepted and approved by the previous rule, which implicitly
    # included the uncited summary despite the cited bullet saying 1,200 users.
    application.answers = [Answer(
        question_id=question.id, text="Built a Flask REST API in Python serving 5,000 users.",
        origin="ai_draft", sources=["exp-1-b1"], at=utcnow(),
    )]
    application.approval = Approval(
        content_hash=content_hash(package.resolved_package(job, application, candidate)),
        approved_at=utcnow(), profile_revision=candidate.revision, job_revision=job.revision,
    )
    application.review_state = ReviewState.APPROVED.value
    session.commit()

    assert "5000" in " ".join(package.check(job, application, candidate)[0])
    assert package.review_state(job, application, candidate) is ReviewState.DRAFT
    token = package.package_token(job, application, candidate)
    with pytest.raises(package.PackageError, match="The package is Draft"):
        package.record_applied(session, job, candidate, token)
    assert application.submitted_snapshots == []
    assert application.tracking_status == "saved"
