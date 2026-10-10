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


def test_current_approvals_read_their_state_without_rerunning_the_package_check(session, sample_profile, monkeypatch):
    from tests.test_package import accept_resume, approve

    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    accept_resume(session, job, candidate)
    approval = approve(session, job, candidate)
    assert job.application.approval_rules == package.APPROVAL_RULES

    calls = []
    original = package.check
    monkeypatch.setattr(package, "check", lambda *args: calls.append(1) or original(*args))
    assert package.review_state(job, job.application, candidate) is ReviewState.APPROVED
    assert calls == []

    # The same approval recorded under older rules is checked again.
    job.application.approval_rules = package.APPROVAL_RULES - 1
    assert package.review_state(job, job.application, candidate) is ReviewState.APPROVED
    assert calls == [1]


OLD_APPROVAL_KEYS = {"content_hash", "approved_at", "profile_revision", "job_revision", "warnings"}


def test_stored_rules_key_is_read_and_removed_at_startup(settings, sample_profile):
    """One release stored "rules" inside the approval JSON; earlier versions reject that key."""
    import json

    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app.main import create_app
    from tests.conftest import BASE_URL
    from tests.test_package import accept_resume, approve

    with TestClient(create_app(settings), base_url=BASE_URL) as first:
        with first.app.state.session_factory() as session:
            candidate = profile.save(session, sample_profile).candidate
            job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
            accept_resume(session, job, candidate)
            approve(session, job, candidate)
            stored = json.loads(session.execute(text("SELECT approval FROM applications")).scalar_one())
            assert set(stored) == OLD_APPROVAL_KEYS
            session.execute(text("UPDATE applications SET approval = :a"),
                            {"a": json.dumps(stored | {"rules": 2})})
            session.commit()
            session.expire_all()  # read it back from the database
            assert package.review_state(job, job.application, candidate) is ReviewState.APPROVED

    with TestClient(create_app(settings), base_url=BASE_URL) as restarted:
        with restarted.app.state.session_factory() as session:
            stored = json.loads(session.execute(text("SELECT approval FROM applications")).scalar_one())
            assert set(stored) == OLD_APPROVAL_KEYS


def test_existing_database_gains_the_approval_rules_column(tmp_path):
    from sqlalchemy import inspect, text

    from app.db import init_db, make_engine, make_session_factory

    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    init_db(engine)
    with make_session_factory(engine)() as session:
        jobs.create(session, jobs.clean_input(**DEMO_JOB))
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE applications DROP COLUMN approval_rules"))
    init_db(engine)
    assert "approval_rules" in {c["name"] for c in inspect(engine).get_columns("applications")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT approval_rules FROM applications")).scalar_one() == 1
    engine.dispose()


def _old_approval(session, job, candidate):
    """An approval recorded before the rules version existed."""
    application = job.application
    application.approval = Approval(
        content_hash=content_hash(package.resolved_package(job, application, candidate)),
        approved_at=utcnow(), profile_revision=candidate.revision, job_revision=job.revision,
    )
    application.approval_rules = 1
    application.review_state = ReviewState.APPROVED.value
    session.commit()


def test_startup_marks_old_approvals_that_still_pass(session, sample_profile, monkeypatch):
    from tests.test_package import accept_resume

    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    accept_resume(session, job, candidate)
    _old_approval(session, job, candidate)

    assert package.upgrade_stored_approvals(session) == 1
    assert job.application.approval is not None
    assert job.application.approval_rules == package.APPROVAL_RULES
    calls = []
    original = package.check
    monkeypatch.setattr(package, "check", lambda *args: calls.append(1) or original(*args))
    assert package.review_state(job, job.application, candidate) is ReviewState.APPROVED
    assert calls == []
    assert package.upgrade_stored_approvals(session) == 0  # nothing left to settle


def test_startup_removes_old_approvals_that_no_longer_pass(session, sample_profile):
    sample_profile["summary"] = "Previously supported 5,000 users on legacy services."
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    record = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    resume.accept(session, job, candidate, resume.proposal_token(record))
    question = answers.add_question(session, job, "Describe a project")
    job.application.answers = [Answer(
        question_id=question.id, text="Built a Flask REST API in Python serving 5,000 users.",
        origin="ai_draft", sources=["exp-1-b1"], at=utcnow(),
    )]
    _old_approval(session, job, candidate)

    assert package.upgrade_stored_approvals(session) == 1
    assert job.application.approval is None
    assert job.application.review_state == ReviewState.DRAFT.value
    assert package.review_state(job, job.application, candidate) is ReviewState.DRAFT


def test_startup_leaves_stale_old_approvals_alone(session, sample_profile):
    from tests.test_package import accept_resume

    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    accept_resume(session, job, candidate)
    _old_approval(session, job, candidate)
    edited = jobs.clean_input(**(DEMO_JOB | {"title": "Changed title"}))
    jobs.update(session, job, edited)

    assert package.upgrade_stored_approvals(session) == 0
    application = job.application
    assert application.approval is not None and not package._checked(application)
    assert application.approval_rules_for == "seen:" + package.approval_key(application.approval)
    assert package.review_state(job, application, candidate) is ReviewState.STALE


def test_startup_does_not_revisit_approvals_it_has_seen(session, sample_profile, monkeypatch):
    from tests.test_package import accept_resume

    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    accept_resume(session, job, candidate)
    _old_approval(session, job, candidate)
    jobs.update(session, job, jobs.clean_input(**(DEMO_JOB | {"title": "Changed title"})))  # now Stale
    package.upgrade_stored_approvals(session)

    states = []
    original = package._recorded_state
    monkeypatch.setattr(package, "_recorded_state", lambda *args: states.append(1) or original(*args))
    package.upgrade_stored_approvals(session)
    assert states == []


def test_a_seen_approval_that_reads_approved_again_is_still_checked(session, sample_profile):
    """Marking an approval as seen never vouches for it."""
    sample_profile["summary"] = "Previously supported 5,000 users on legacy services."
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    record = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    resume.accept(session, job, candidate, resume.proposal_token(record))
    question = answers.add_question(session, job, "Describe a project")
    supported = Answer(question_id=question.id, text="Built a Flask REST API in Python serving 5,000 users.",
                       origin="ai_draft", sources=["exp-1-b1"], at=utcnow())
    job.application.answers = [supported]
    _old_approval(session, job, candidate)
    job.application.answers = []  # edited after approval: reads Draft at startup
    session.commit()
    package.upgrade_stored_approvals(session)
    assert job.application.approval_rules_for.startswith("seen:")

    job.application.answers = [supported]  # the edit is reverted: the hash matches again
    session.commit()
    assert package.review_state(job, job.application, candidate) is ReviewState.DRAFT


def test_rules_stamp_does_not_carry_over_to_an_approval_recorded_elsewhere(session, sample_profile):
    """An earlier app version clears and records approvals without updating the stamp columns."""
    from tests.test_package import accept_resume, approve

    sample_profile["summary"] = "Previously supported 5,000 users on legacy services."
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    accept_resume(session, job, candidate)
    approve(session, job, candidate)
    application = job.application
    assert application.approval_rules == package.APPROVAL_RULES and application.approval_rules_for

    # As an earlier version would: accept an answer its looser rules allow and approve again,
    # leaving approval_rules and approval_rules_for as they were.
    question = answers.add_question(session, job, "Describe a project")
    application.answers = [Answer(
        question_id=question.id, text="Built a Flask REST API in Python serving 5,000 users.",
        origin="ai_draft", sources=["exp-1-b1"], at=utcnow(),
    )]
    application.approval = Approval(
        content_hash=content_hash(package.resolved_package(job, application, candidate)),
        approved_at=utcnow(), profile_revision=candidate.revision, job_revision=job.revision,
    )
    session.commit()
    assert application.approval_rules == package.APPROVAL_RULES  # the stamp is still there

    assert package.review_state(job, application, candidate) is ReviewState.DRAFT
    assert package.upgrade_stored_approvals(session) == 1
    assert application.approval is None


def test_snapshots_with_a_stored_rules_key_are_readable(session, sample_profile):
    import json

    from sqlalchemy import text

    from tests.test_package import accept_resume, approve, record_applied

    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    accept_resume(session, job, candidate)
    approve(session, job, candidate)
    record_applied(session, job, candidate)
    snapshots = json.loads(session.execute(text("SELECT submitted_snapshots FROM applications")).scalar_one())
    snapshots[0]["approval"]["rules"] = 2
    session.execute(text("UPDATE applications SET submitted_snapshots = :s"), {"s": json.dumps(snapshots)})
    session.commit()
    session.expire_all()
    assert job.application.submitted_snapshots[0].approval.content_hash == snapshots[0]["approval"]["content_hash"]


def test_approvals_built_in_code_still_reject_unknown_fields():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="rules"):
        Approval(content_hash="x", approved_at=utcnow(), profile_revision=1, job_revision=1, rules=2)


def test_startup_approval_check_only_visits_rows_with_an_approval(session, sample_profile, monkeypatch):
    from tests.test_package import accept_resume, approve

    candidate = profile.save(session, sample_profile).candidate
    approved, never, cleared = (jobs.create(session, jobs.clean_input(**DEMO_JOB)) for _ in range(3))
    for job in (approved, cleared):
        accept_resume(session, job, candidate)
        approve(session, job, candidate)
    answers.add_question(session, cleared, "Describe a project")  # clears that approval
    assert never.application.approval is None and cleared.application.approval is None

    visited = []
    original = package._settled
    monkeypatch.setattr(package, "_settled", lambda application: visited.append(application.job_id)
                        or original(application))
    assert package.upgrade_stored_approvals(session) == 0
    assert visited == [approved.id]
