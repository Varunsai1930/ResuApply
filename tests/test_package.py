"""Package checks, approval, review state and submitted snapshots (synthetic data only)."""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from app.db import make_engine, make_session_factory
from app.schemas.tracking import ReviewState, TrackingStatus
from app.services import answers, jobs, package, profile, resume, tracking
from app.services.package import PackageError
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE
from tests.test_answers import confirm

EMAIL_Q = "What is your email address?"
AUTH_Q = "Are you authorized to work in the US?"
GENDER_Q = "What is your gender?"
WHY_Q = "Why do you want this role at Demo Corp?"


@pytest.fixture
def candidate(session):
    return profile.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate


@pytest.fixture
def job(session, candidate):
    return jobs.create(session, jobs.clean_input(**DEMO_JOB))


@pytest.fixture
def app_(job):
    return job.application


def approve(session, job, candidate):
    """Approve the package as the Review section shows it now."""
    return package.approve(session, job, candidate, package.package_token(job, job.application, candidate))


def record_applied(session, job, candidate, **kwargs):
    """Record Applied with the package as the Tracking form shows it now."""
    token = package.package_token(job, job.application, candidate)
    return package.record_applied(session, job, candidate, token, **kwargs)


def accept_resume(session, job, candidate):
    record = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    resume.accept(session, job, candidate, resume.proposal_token(record))
    return record


def ready(session, job, candidate):
    """A fully resolved package: accepted resume, factual, confirmed, open and skipped answers."""
    app_ = job.application
    accept_resume(session, job, candidate)
    answers.add_question(session, job, EMAIL_Q)
    q_auth = answers.add_question(session, job, AUTH_Q)
    q_why = answers.add_question(session, job, WHY_Q)
    q_gender = answers.add_question(session, job, GENDER_Q, required=False)
    confirm(session, job, q_auth.id, candidate)
    answers.set_answer(session, job, app_, q_why.id, "I like building Python services.")
    answers.skip_answer(session, job, app_, q_gender.id)
    return app_


def edit_profile(session, candidate, **changes):
    edited = candidate.profile.model_dump()
    if "bullet" in changes:
        edited["experience"][0]["bullets"][0]["text"] = changes["bullet"]
    if "email" in changes:
        edited["contact"]["email"] = changes["email"]
    if "summary" in changes:
        edited["summary"] = changes["summary"]
    profile.save(session, edited)


# ---------------------------------------------------------------- blockers and warnings

def test_empty_package_is_blocked_on_the_resume(session, job, app_, candidate):
    blockers, warnings = package.check(job, app_, candidate)
    assert any("No accepted resume" in b for b in blockers)
    assert "No requirements were reviewed for this job." in warnings
    with pytest.raises(PackageError) as caught:
        approve(session, job, candidate)
    assert caught.value.details == blockers
    assert app_.approval is None


def test_no_profile_is_a_blocker(session):
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    blockers, _ = package.check(job, job.application, None)
    assert any("Create your profile" in b for b in blockers)
    assert package.review_state(job, job.application, None) is ReviewState.DRAFT


def test_stale_accepted_resume_is_a_blocker(session, job, app_, candidate):
    accept_resume(session, job, candidate)
    assert package.check(job, app_, candidate)[0] == []
    edit_profile(session, candidate, bullet="Built a Flask REST API in Python for 1,200 internal users")
    blockers, _ = package.check(job, app_, candidate)
    assert len(blockers) == 1 and "out of date" in blockers[0]


def test_unresolved_sensitive_questions_block_with_the_question_text(session, job, app_, candidate):
    accept_resume(session, job, candidate)
    answers.add_question(session, job, AUTH_Q)
    answers.add_question(session, job, GENDER_Q, required=False)
    blockers, _ = package.check(job, app_, candidate)
    assert len(blockers) == 2
    assert all("own answer" in b and "explicit skip" in b for b in blockers)
    assert 'Are you authorized to work in the US?' in blockers[0] and "q1" not in blockers[0]
    assert "gender" in blockers[1]  # optional but sensitive: still a blocker


def test_unknown_category_blocks(session, job, app_, candidate):
    accept_resume(session, job, candidate)
    answers.add_question(session, job, "Would you relocate for this job?")
    blockers, _ = package.check(job, app_, candidate)
    assert len(blockers) == 1 and "confirm the question's category" in blockers[0] and "relocate" in blockers[0]


def test_required_question_without_answer_blocks_but_optional_only_warns(session, job, app_, candidate):
    accept_resume(session, job, candidate)
    answers.add_question(session, job, WHY_Q)
    answers.add_question(session, job, "What excites you about our team?", required=False)
    blockers, warnings = package.check(job, app_, candidate)
    assert len(blockers) == 1 and "required question has no accepted answer" in blockers[0]
    assert any("optional question is unanswered" in w and "excites" in w for w in warnings)


def test_long_question_text_is_shortened(session, job, app_, candidate):
    accept_resume(session, job, candidate)
    answers.add_question(session, job, "Why " + "really " * 40 + "this role?")
    blocker = package.check(job, app_, candidate)[0][0]
    assert "…" in blocker and len(blocker) < 200


def test_missing_factual_value_blocks_when_required(session, job, app_, candidate):
    accept_resume(session, job, candidate)
    edited = candidate.profile.model_dump()
    edited["contact"]["links"]["github"] = None
    profile.save(session, edited)
    accept_resume(session, job, candidate)
    answers.add_question(session, job, "What is your GitHub profile?")
    blockers, _ = package.check(job, app_, candidate)
    assert len(blockers) == 1 and "required question has no accepted answer" in blockers[0]


def test_warnings_for_pending_drafts_newer_proposal_and_no_requirements(session, job, app_, candidate):
    accept_resume(session, job, candidate)
    question = answers.add_question(session, job, "What excites you about our team?", required=False)
    from tests.test_answers import make_draft
    answers.replace_drafts(app_, [make_draft(question.id)])
    session.commit()
    resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    blockers, warnings = package.check(job, app_, candidate)
    assert blockers == []
    assert any("AI answer drafts waiting" in w and "excites" in w for w in warnings)
    assert any("newer resume proposal" in w for w in warnings)
    assert any("No requirements were reviewed" in w for w in warnings)


# ---------------------------------------------------------------- approval and review state

def test_approval_records_hash_revisions_and_warnings_and_leaves_tracking_alone(session, job, app_, candidate):
    ready(session, job, candidate)
    before = (app_.status, list(app_.status_history), app_.applied_on)
    approval = approve(session, job, candidate)
    assert app_.approval == approval and app_.review_state == "approved"
    assert approval.profile_revision == candidate.revision and approval.job_revision == job.revision
    assert approval.warnings == ["No requirements were reviewed for this job."]
    assert approval.content_hash and approval.approved_at is not None
    assert (app_.status, list(app_.status_history), app_.applied_on) == before
    assert app_.status is TrackingStatus.SAVED
    assert package.review_state(job, app_, candidate) is ReviewState.APPROVED


def test_resolved_package_view(session, job, app_, candidate):
    ready(session, job, candidate)
    view = package.resolved_package(job, app_, candidate)
    assert view["resume"]["contact"]["email"] == "jordan@example.com"
    by_question = {q["question"]: q for q in view["questions"]}
    assert by_question[EMAIL_Q] | {"id": "q1"} == {
        "id": "q1", "question": EMAIL_Q, "required": True, "category": "factual",
        "label": "From profile", "text": "jordan@example.com", "resolved": True, "skipped": False}
    assert by_question[AUTH_Q]["label"] == "From profile (confirmed)" and by_question[AUTH_Q]["text"] == "Yes"
    assert by_question[GENDER_Q]["skipped"] is True and by_question[GENDER_Q]["text"] == ""
    assert package.resolved_package(job, app_, None)["resume"] is None


def test_review_state_survives_reload_and_sync_stores_it(session, job, app_, candidate, settings):
    ready(session, job, candidate)
    approve(session, job, candidate)
    engine = make_engine(settings.database_url)
    with make_session_factory(engine)() as other:
        other_job, other_candidate = jobs.get(other, job.id), profile.get_candidate(other)
        assert package.review_state(other_job, other_job.application, other_candidate) is ReviewState.APPROVED
    engine.dispose()
    edit_profile(session, candidate, email="jordan.new@example.com")
    assert app_.review_state == "approved"  # the column is only updated when synced
    assert package.sync_review_state(session, job, app_, candidate) is ReviewState.STALE
    assert app_.review_state == "stale"


def test_editing_an_answer_returns_to_draft(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    answers.set_answer(session, job, app_, "q3", "A different answer.")
    assert app_.approval is None and app_.review_state == "draft"
    assert package.review_state(job, app_, candidate) is ReviewState.DRAFT


def test_adding_a_question_returns_to_draft(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    answers.add_question(session, job, "What excites you about our team?", required=False)
    assert package.review_state(job, app_, candidate) is ReviewState.DRAFT


def test_accepting_a_different_resume_returns_to_draft(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    record = resume.propose(session, job, candidate, {"summary": None, "experience": [], "skills": ["Python"]})
    assert app_.review_state == "approved" and app_.approval is not None
    resume.accept(session, job, candidate, resume.proposal_token(record))
    assert app_.approval is None and app_.review_state == "draft"
    assert package.review_state(job, app_, candidate) is ReviewState.DRAFT


def test_a_new_proposal_alone_keeps_approval(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    resume.propose(session, job, candidate, {"experience": []})
    assert app_.review_state == "approved" and app_.approval is not None
    assert package.review_state(job, app_, candidate) is ReviewState.APPROVED
    assert any("newer resume proposal" in w for w in package.check(job, app_, candidate)[1])


def test_a_stored_draft_keeps_approval(session, job, app_, candidate):
    ready(session, job, candidate)
    question = answers.add_question(session, job, "What excites you about our team?", required=False)
    approve(session, job, candidate)
    from tests.test_answers import make_draft
    answers.replace_drafts(app_, [make_draft(question.id)])
    session.commit()
    # adding the question above cleared nothing: it came before approval
    assert package.review_state(job, app_, candidate) is ReviewState.APPROVED


def test_profile_edit_makes_it_stale(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    edit_profile(session, candidate, summary="Computer science student focused on backend services.")
    assert package.review_state(job, app_, candidate) is ReviewState.STALE


def test_job_description_edit_makes_it_stale(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    jobs.update(session, job, jobs.clean_input(**(DEMO_JOB | {"description": DEMO_JOB["description"] + "\nMore."})))
    assert package.review_state(job, app_, candidate) is ReviewState.STALE


def test_stale_wins_over_changed_content(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    edit_profile(session, candidate, email="jordan.new@example.com")  # changes the email answer too
    assert package.review_state(job, app_, candidate) is ReviewState.STALE


# ---------------------------------------------------------------- recording Applied

@pytest.mark.parametrize("how", ["draft", "stale"])
def test_record_applied_refuses_draft_and_stale_packages(session, job, app_, candidate, how):
    ready(session, job, candidate)
    approve(session, job, candidate)
    if how == "draft":
        answers.set_answer(session, job, app_, "q3", "Changed.")
    else:
        edit_profile(session, candidate, email="jordan.new@example.com")
    with pytest.raises(PackageError, match="Approve the current package"):
        record_applied(session, job, candidate)
    assert app_.status is TrackingStatus.SAVED and app_.applied_on is None
    assert app_.submitted_snapshots == []
    assert [e.status for e in app_.status_history] == [TrackingStatus.SAVED]


def test_record_applied_without_approval_is_refused(session, job, app_, candidate):
    ready(session, job, candidate)
    with pytest.raises(PackageError, match="Draft"):
        record_applied(session, job, candidate)
    assert app_.status is TrackingStatus.SAVED


def test_record_applied_stores_a_snapshot_and_sets_the_status(session, job, app_, candidate):
    ready(session, job, candidate)
    approval = approve(session, job, candidate)
    on = date.today() - timedelta(days=2)
    snapshot = record_applied(session, job, candidate, on=on, note="Submitted on their site")
    assert app_.status is TrackingStatus.APPLIED and app_.applied_on == on
    assert app_.status_history[-1].note == "Submitted on their site" and app_.status_history[-1].on == on
    assert app_.submitted_snapshots == [snapshot] and package.get_snapshot(app_, 1) == snapshot
    assert package.get_snapshot(app_, 2) is None
    assert snapshot.id == 1 and snapshot.submitted_on == on and snapshot.approval == approval
    assert snapshot.profile_revision == candidate.revision and snapshot.job_revision == job.revision
    assert snapshot.job == {"title": job.title, "company": job.company, "location": job.location,
                            "url": job.url, "description": job.description, "revision": job.revision}
    assert snapshot.profile == candidate.profile and snapshot.profile is not candidate.profile
    assert snapshot.resume["contact"]["email"] == "jordan@example.com"
    assert [a["question"] for a in snapshot.answers] == [EMAIL_Q, AUTH_Q, WHY_Q, GENDER_Q]
    assert set(snapshot.answers[0]) == {"question", "required", "category", "label", "text"}
    html = snapshot.resume_html
    assert html.lstrip().startswith('<main class="resume-document"') and html.rstrip().endswith("</main>")
    assert "jordan@example.com" in html and "Sample Analytics" in html
    assert "csrf" not in html.lower() and "<html" not in html and "href=" not in html
    assert app_.review_state == "approved"


def test_failed_status_change_stores_no_snapshot(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    with pytest.raises(tracking.TrackingError, match="future"):
        record_applied(session, job, candidate, on=date.today() + timedelta(days=1))
    assert app_.submitted_snapshots == [] and app_.status is TrackingStatus.SAVED
    record_applied(session, job, candidate)
    with pytest.raises(tracking.TrackingError, match="already"):
        record_applied(session, job, candidate)
    assert len(app_.submitted_snapshots) == 1


def test_recording_applied_without_a_package_still_works(session, job, app_):
    tracking.change_status(session, app_, "applied", note="Applied elsewhere")
    assert app_.status is TrackingStatus.APPLIED and app_.submitted_snapshots == []


def test_second_applied_snapshot_gets_the_next_id(session, job, app_, candidate):
    ready(session, job, candidate)
    approve(session, job, candidate)
    first = record_applied(session, job, candidate)
    tracking.change_status(session, app_, "withdrawn")
    answers.set_answer(session, job, app_, "q3", "A better answer.")
    with pytest.raises(PackageError):
        record_applied(session, job, candidate)
    approve(session, job, candidate)
    second = record_applied(session, job, candidate)
    assert (first.id, second.id) == (1, 2)
    assert app_.status is TrackingStatus.APPLIED
    assert [s.id for s in app_.submitted_snapshots] == [1, 2]
    assert first.answers[2]["text"] == "I like building Python services."
    assert second.answers[2]["text"] == "A better answer."


# ---------------------------------------------------------------- acceptance gate

def test_acceptance_gate_unchanged_package_after_profile_and_job_edits(session, job, app_, candidate, settings):
    """Prepare, approve, submit manually and retrieve an unchanged package after profile edits."""
    ready(session, job, candidate)
    approve(session, job, candidate)
    record_applied(session, job, candidate)
    stored = app_.submitted_snapshots[0]
    frozen = stored.model_dump_json()
    html, email = stored.resume_html, "jordan@example.com"
    assert email in html and "Wrote unit tests for the billing module" in html

    edit_profile(session, candidate, bullet="Wrote integration tests for the billing module",
                 email="jordan.changed@example.com", summary="A completely different summary.")
    jobs.update(session, job, jobs.clean_input(**(DEMO_JOB | {"description": "Entirely new description."})))
    assert package.sync_review_state(session, job, app_, candidate) is ReviewState.STALE
    assert app_.review_state == "stale"

    # Reload everything from a fresh engine on the same database file.
    engine = make_engine(settings.database_url)
    with make_session_factory(engine)() as fresh:
        reloaded_job = jobs.get(fresh, job.id)
        reloaded_candidate = profile.get_candidate(fresh)
        snapshot = package.get_snapshot(reloaded_job.application, 1)
        assert snapshot.model_dump_json() == frozen
        assert snapshot.resume_html == html
        assert snapshot.resume["contact"]["email"] == email
        assert snapshot.profile.contact.email == email
        assert snapshot.profile.experience[0].bullets[2].text == "Wrote unit tests for the billing module"
        assert snapshot.job["description"] == DEMO_JOB["description"]
        assert [a["text"] for a in snapshot.answers][:2] == [email, "Yes"]
        assert package.review_state(reloaded_job, reloaded_job.application, reloaded_candidate) is ReviewState.STALE
        assert reloaded_candidate.profile.contact.email == "jordan.changed@example.com"
    engine.dispose()
