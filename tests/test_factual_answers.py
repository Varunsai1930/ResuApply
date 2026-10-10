"""Factual answers: length limits, name parts and corrected resolution of saved data (synthetic data only)."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import text

from app.db import init_db, make_engine, make_session_factory, utcnow
from app.schemas.package import Approval, Question
from app.schemas.tracking import ReviewState
from app.services import answers as svc
from app.services import jobs as job_service
from app.services import package
from app.services import profile as profile_service
from app.services.answers import AnswerError
from app.services.package import PackageError
from app.services.text import content_hash
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE
from tests.test_answers import confirm
from tests.test_package import accept_resume, approve, record_applied
from tests.test_questions_routes import add, approve_package, post, qid_of, ready_job, review_part, row, workspace

LONG_EMAIL = "jordan.with.a.much.longer.address@example.com"


@pytest.fixture
def candidate(session):
    return profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate


@pytest.fixture
def job(session, candidate):
    created = job_service.create(session, job_service.clean_input(**DEMO_JOB))
    accept_resume(session, created, candidate)
    return created


def resolved(job, candidate, qid):
    return next(r for r in svc.resolve_all(job, job.application, candidate.profile) if r.question.id == qid)


def set_email(session, candidate, email):
    edited = candidate.profile.model_dump()
    edited["contact"]["email"] = email
    profile_service.save(session, edited)


def legacy_question(session, job, qid, text, key="name") -> Question:
    """A question as an earlier version saved it: "First name" stored with the full-name key."""
    question = Question(id=qid, text=text, category="factual", detected_category="factual", factual_key=key,
                        added_at=utcnow())
    job.questions = [*job.questions, question]
    job.question_counter = max(job.question_counter, int(qid[1:]))
    session.commit()
    return question


# ---------------------------------------------------------------- length limits

@pytest.mark.parametrize("question,limit,unit,problem", [
    ("What is your email address?", 10, "chars", "18 chars exceeds the limit of 10"),
    ("Where are you currently located?", 1, "words", "2 words exceeds the limit of 1"),
])
def test_an_over_limit_profile_value_stays_visible_and_unresolved(session, job, candidate, question, limit, unit, problem):
    q = svc.add_question(session, job, question, limit=limit, limit_unit=unit)
    item = resolved(job, candidate, q.id)
    assert (item.label, item.resolved, item.problem) == ("Over the length limit", False, problem)
    assert item.text == item.value and item.text  # shown in full, never shortened
    with pytest.raises(AnswerError, match="too long") as refused:
        confirm(session, job, q.id, candidate)
    assert problem in str(refused.value) and "Nothing is shortened" in str(refused.value)
    assert job.application.answers == []


def test_sensitive_factual_values_are_checked_against_the_limit_too(session, job, candidate):
    q = svc.add_question(session, job, "Are you authorized to work in the US?", limit=2)
    item = resolved(job, candidate, q.id)
    assert (item.label, item.resolved, item.text) == ("Over the length limit", False, "Yes")
    with pytest.raises(AnswerError, match="3 chars exceeds the limit of 2"):
        confirm(session, job, q.id, candidate)


def test_a_later_profile_edit_over_the_limit_blocks_even_an_optional_question(session, job, candidate):
    q = svc.add_question(session, job, "What is your email address?", required=False, limit=25)
    confirm(session, job, q.id, candidate)
    assert resolved(job, candidate, q.id).resolved
    approve(session, job, candidate)

    set_email(session, candidate, LONG_EMAIL)
    accept_resume(session, job, candidate)  # the profile changed; the resume is accepted again
    item = resolved(job, candidate, q.id)
    assert (item.label, item.resolved, item.text) == ("Over the length limit", False, LONG_EMAIL)
    blockers, warnings = package.check(job, job.application, candidate)
    assert len(blockers) == 1 and "45 chars exceeds the limit of 25" in blockers[0]
    assert "skip this optional question" in blockers[0]
    assert not any("optional question is unanswered" in w for w in warnings)
    with pytest.raises(PackageError) as refused:
        approve(session, job, candidate)
    assert any("exceeds the limit" in d for d in refused.value.details)

    # A manual replacement, or an explicit skip, resolves it.
    svc.set_answer(session, job, job.application, q.id, "jordan@example.com")
    assert resolved(job, candidate, q.id).resolved
    svc.skip_answer(session, job, job.application, q.id)
    assert resolved(job, candidate, q.id).skipped
    approve(session, job, candidate)


def test_a_required_over_limit_value_needs_a_manual_answer(session, job, candidate):
    q = svc.add_question(session, job, "What is your email address?", limit=10)
    with pytest.raises(AnswerError, match="required"):
        svc.skip_answer(session, job, job.application, q.id)
    with pytest.raises(AnswerError, match="exceeds the limit of 10"):
        svc.set_answer(session, job, job.application, q.id, "jordan@example.com")
    svc.set_answer(session, job, job.application, q.id, "jordan@ex")
    assert resolved(job, candidate, q.id).label == "User answer"


def test_over_limit_rows_offer_a_manual_answer_or_a_skip_over_http(client, sample_profile):
    job_id = ready_job(client, sample_profile)
    required = qid_of(add(client, job_id, "What is your email address?", limit="10"))
    optional = qid_of(add(client, job_id, "Where are you currently located?", required=False, limit="1",
                          limit_unit="words"))
    page = workspace(client, job_id)
    for qid, value, problem in ((required, "jordan@example.com", "18 chars exceeds the limit of 10"),
                                (optional, "Austin, TX", "2 words exceeds the limit of 1")):
        shown = row(page, qid)
        assert "Over the length limit" in shown and problem in shown and value in shown
        assert "ResuApply never shortens it" in shown
        assert "/confirm" not in shown and "<details open><summary>Answer it myself instead" in shown
    assert "Skip this optional question" in row(page, optional)
    assert "Skip this optional question" not in row(page, required)
    review = review_part(workspace(client, job_id, "review"))
    assert "2 words exceeds the limit of 1" in review and "Approve package</button>" not in review
    refused = post(client, job_id, optional, "confirm", confirmation_token="0")
    assert refused.status_code == 422 and "too long" in refused.text
    assert post(client, job_id, optional, "skip").status_code == 303
    assert post(client, job_id, required, "answer", text="j@ex.com").status_code == 303
    assert approve_package(client, job_id).status_code == 303


# ---------------------------------------------------------------- name parts

def test_name_parts_need_a_manual_answer(session, job, candidate):
    first = svc.add_question(session, job, "First name")
    last = svc.add_question(session, job, "Last name", required=False)
    full = svc.add_question(session, job, "Full name")
    assert (first.factual_key, last.factual_key, full.factual_key) == ("first_name", "last_name", "name")
    assert resolved(job, candidate, first.id).label == "Missing information"
    assert resolved(job, candidate, full.id).text == "Jordan Example"
    with pytest.raises(AnswerError, match="never splits it"):
        confirm(session, job, first.id, candidate)
    blockers, warnings = package.check(job, job.application, candidate)
    assert len(blockers) == 1 and "First name" in blockers[0]
    assert any("Last name" in w for w in warnings)
    svc.set_answer(session, job, job.application, first.id, "Jordan")
    assert (resolved(job, candidate, first.id).label, resolved(job, candidate, first.id).text) == ("User answer", "Jordan")


def test_legacy_name_mappings_are_read_through_their_question_text(session, job, candidate):
    first = legacy_question(session, job, "q1", "What is your legal first name?")
    surname = legacy_question(session, job, "q2", "Surname")
    full = legacy_question(session, job, "q3", "Full name")
    for q in (first, surname):
        item = resolved(job, candidate, q.id)
        assert (item.label, item.resolved, item.text) == ("Missing information", False, "")
        with pytest.raises(AnswerError, match="never splits it"):
            confirm(session, job, q.id, candidate)
    assert resolved(job, candidate, full.id).text == "Jordan Example"
    assert svc.add_question(session, job, "Given name").id == "q4"  # the counter continues past saved IDs


def test_name_part_rows_show_a_manual_entry_instruction(client, sample_profile):
    job_id = ready_job(client, sample_profile)
    first = qid_of(add(client, job_id, "First name"))
    shown = row(workspace(client, job_id), first)
    assert "Type this part of your name below" in shown and "never splits it" in shown
    assert 'href="/profile/edit"' not in shown and "Use the profile value" not in shown
    assert "<details open><summary>Answer it myself instead" in shown


# ---------------------------------------------------------------- saved data under the corrected rules

def test_corrected_resolution_invalidates_an_approval_that_no_longer_matches(session, job, candidate):
    """An approval recorded while "First name" resolved to the full name no longer applies."""
    legacy_question(session, job, "q1", "First name")
    app_ = job.application
    view = package.resolved_package(job, app_, candidate)
    view["questions"][0] |= {"label": "From profile", "text": "Jordan Example", "resolved": True}
    app_.approval = Approval(content_hash=content_hash(view), approved_at=utcnow(),
                             profile_revision=candidate.revision, job_revision=job.revision)
    app_.review_state = ReviewState.APPROVED.value
    session.commit()

    assert package.review_state(job, app_, candidate) is ReviewState.DRAFT
    with pytest.raises(PackageError, match="The package is Draft"):
        record_applied(session, job, candidate)
    assert app_.submitted_snapshots == [] and app_.tracking_status == "saved"


def test_existing_databases_load_unchanged_and_snapshots_stay_frozen(session, job, candidate, settings):
    q = svc.add_question(session, job, "Tell us whether you are authorized to work in Canada")
    svc.set_answer(session, job, job.application, q.id, "Yes")
    approve(session, job, candidate)
    record_applied(session, job, candidate)
    # As an earlier version froze it: the US record's answer, confirmed for a question about Canada.
    snapshot = job.application.submitted_snapshots[0]
    old = snapshot.model_copy(update={"answers": [snapshot.answers[0] | {"label": "From profile (confirmed)"}]})
    job.application.submitted_snapshots = [old]
    session.commit()
    frozen = old.model_dump_json()

    def schema(engine):
        with engine.connect() as conn:
            return conn.execute(text("SELECT type, name, sql FROM sqlite_master ORDER BY name")).all()

    engine = make_engine(settings.database_url)
    before = schema(engine)
    init_db(engine)
    assert schema(engine) == before  # nothing to migrate
    with make_session_factory(engine)() as fresh:
        reloaded = job_service.get(fresh, job.id)
        assert reloaded.application.submitted_snapshots[0].model_dump_json() == frozen
        assert [q.factual_key for q in reloaded.questions] == ["authorization"]
        fresh_candidate = profile_service.get_candidate(fresh)
        assert svc.resolve_all(reloaded, reloaded.application, fresh_candidate.profile)[0].label == "User answer"
    engine.dispose()
