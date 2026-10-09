"""Questions, answers, approval clearing, resolution labels and the answer bank (synthetic data only)."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy.orm import object_session

from app.db import utcnow
from app.models import AnswerBankEntry
from app.schemas.package import Approval, AnswerDraft
from app.schemas.profile import Authorization
from app.services import answers as svc
from app.services import jobs as job_service
from app.services import profile as profile_service
from app.services.answers import AnswerError
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE


@pytest.fixture
def candidate(session):
    return profile_service.save(session, copy.deepcopy(SAMPLE_PROFILE)).candidate


@pytest.fixture
def profile(candidate):
    return candidate.profile


@pytest.fixture
def job(session, candidate):
    return job_service.create(session, job_service.clean_input(**DEMO_JOB))


@pytest.fixture
def app_(job):
    return job.application


def approve(app_):
    app_.approval = Approval(content_hash="abc", approved_at=utcnow(), profile_revision=1, job_revision=1)
    app_.review_state = "approved"
    object_session(app_).commit()


def set_drafts(app_, *drafts):
    app_.answer_drafts = list(drafts)
    object_session(app_).commit()


def confirm(session, job, qid, candidate):
    return svc.confirm_answer(session, job, job.application, qid, candidate)


def accept(session, job, qid, candidate):
    return svc.accept_draft(session, job, job.application, qid, candidate)


def make_draft(qid, text="Built a Flask REST API in Python", sources=("exp-1-b1",)) -> AnswerDraft:
    return AnswerDraft(question_id=qid, text=text, sources=list(sources), model="m", prompt_revision="p",
                       profile_revision=1, job_revision=1, created_at=utcnow())


def add(session, job, text, **kwargs):
    return svc.add_question(session, job, text, **kwargs)


# ---------------------------------------------------------------- questions

def test_add_question_detects_category_and_issues_ids(session, job):
    q1 = add(session, job, "  What is your email?  ")
    q2 = add(session, job, "Why do you want this role?", required=False, limit=100, limit_unit="words")
    q3 = add(session, job, "Are you authorized to work in the US?")
    assert (q1.id, q1.text, q1.category, q1.detected_category, q1.factual_key) == ("q1", "What is your email?", "factual", "factual", "email")
    assert (q2.id, q2.category, q2.factual_key, q2.required, q2.limit, q2.limit_unit) == ("q2", "open", None, False, 100, "words")
    assert (q3.category, q3.factual_key) == ("sensitive_factual", "authorization")
    assert [q.id for q in job.questions] == ["q1", "q2", "q3"] and job.question_counter == 3


def test_question_ids_are_never_reused(session, job, app_):
    add(session, job, "Why us?")
    q2 = add(session, job, "Why this team?")
    svc.remove_question(session, job, app_, q2.id)
    assert add(session, job, "What excites you?").id == "q3"
    svc.remove_question(session, job, app_, "q1")
    assert add(session, job, "Another?").id == "q4"
    assert [q.id for q in job.questions] == ["q3", "q4"]


@pytest.mark.parametrize("kwargs,message", [
    ({"text": "   "}, "Enter the question"),
    ({"text": "x" * 2001}, "under 2,000"),
    ({"text": "Why?", "limit": 0}, "at least 1"),
    ({"text": "Why?", "limit": -5}, "at least 1"),
    ({"text": "Why?", "limit": 1.5}, "at least 1"),
    ({"text": "Why?", "limit_unit": "lines"}, "characters or words"),
    ({"text": "Why?", "category": "bogus"}, "Category must be"),
])
def test_add_question_validation(session, job, kwargs, message):
    with pytest.raises(AnswerError, match=message):
        add(session, job, **kwargs)
    assert job.questions == [] and job.question_counter == 0


def test_detected_sensitive_question_cannot_become_open_or_factual(session, job, app_):
    with pytest.raises(AnswerError, match="cannot become"):
        add(session, job, "What is your gender?", category="open")
    with pytest.raises(AnswerError, match="cannot become"):
        add(session, job, "Are you authorized to work in the US?", category="open")
    with pytest.raises(AnswerError, match="cannot become"):
        add(session, job, "Will you require sponsorship?", category="factual")
    assert job.questions == []
    q = add(session, job, "What is your gender?")
    for category in ("open", "factual", "sensitive_factual", "unknown"):
        with pytest.raises(AnswerError):
            svc.set_category(session, job, app_, q.id, category)
    assert job.questions[0].category == "sensitive"


def test_sensitive_factual_may_become_sensitive_but_stays_the_detected_one(session, job, app_):
    q = add(session, job, "Are you authorized to work in the US?")
    changed = svc.set_category(session, job, app_, q.id, "sensitive")
    assert (changed.category, changed.factual_key, changed.detected_category) == ("sensitive", None, "sensitive_factual")
    back = svc.set_category(session, job, app_, q.id, "sensitive_factual")
    assert (back.category, back.factual_key) == ("sensitive_factual", "authorization")


def test_factual_categories_need_a_matching_profile_field(session, job, app_):
    with pytest.raises(AnswerError, match="No profile field"):
        add(session, job, "Why us?", category="factual")
    q = add(session, job, "Are you willing to relocate?")
    assert q.category == "unknown"
    for category in ("factual", "sensitive_factual"):
        with pytest.raises(AnswerError, match="No profile field"):
            svc.set_category(session, job, app_, q.id, category)
    with pytest.raises(AnswerError, match="Choose a category"):
        svc.set_category(session, job, app_, q.id, "unknown")
    assert svc.set_category(session, job, app_, q.id, "open").category == "open"


def test_override_at_add_time_keeps_what_the_rules_detected(session, job):
    q = add(session, job, "Why us?", category="sensitive")
    assert (q.category, q.detected_category) == ("sensitive", "open")
    q = add(session, job, "What is your major?", category="open")
    assert (q.category, q.detected_category, q.factual_key) == ("open", "factual", None)


def test_set_category_and_remove_drop_that_questions_answer_and_draft(session, job, app_):
    keep = add(session, job, "Why us?")
    q = add(session, job, "Describe a project")
    svc.set_answer(session, job, app_, keep.id, "Because")
    svc.set_answer(session, job, app_, q.id, "A bot")
    set_drafts(app_, make_draft(q.id), make_draft(keep.id))
    svc.set_category(session, job, app_, q.id, "sensitive")
    assert [a.question_id for a in app_.answers] == [keep.id]
    assert [d.question_id for d in app_.answer_drafts] == [keep.id]
    svc.remove_question(session, job, app_, keep.id)
    assert app_.answers == [] and app_.answer_drafts == []
    with pytest.raises(AnswerError, match="no question"):
        svc.remove_question(session, job, app_, "q9")


def test_unchanged_category_keeps_answers_and_approval(session, job, app_):
    q = add(session, job, "Why us?")
    svc.set_answer(session, job, app_, q.id, "Because")
    approve(app_)
    svc.set_category(session, job, app_, q.id, "open")
    assert len(app_.answers) == 1 and app_.approval is not None


# ---------------------------------------------------------------- answers

def test_required_questions_cannot_be_skipped_but_optional_ones_can(session, job, app_):
    required = add(session, job, "Why us?")
    optional = add(session, job, "Anything else?", required=False)
    with pytest.raises(AnswerError, match="required"):
        svc.skip_answer(session, job, app_, required.id)
    skipped = svc.skip_answer(session, job, app_, optional.id)
    assert skipped.skipped and skipped.text == ""
    with pytest.raises(AnswerError, match="empty"):
        svc.set_answer(session, job, app_, required.id, "   ")


@pytest.mark.parametrize("unit,limit,fits,over", [
    ("chars", 20, "x" * 20, "x" * 21),
    ("words", 5, "one two three four five", "one two three four five six"),
])
def test_length_limits_in_characters_and_words(session, job, app_, unit, limit, fits, over):
    q = add(session, job, "Why us?", limit=limit, limit_unit=unit)
    with pytest.raises(AnswerError, match=f"exceeds the limit of {limit}"):
        svc.set_answer(session, job, app_, q.id, over)
    assert app_.answers == []
    assert svc.set_answer(session, job, app_, q.id, fits).text == fits


def test_set_answer_replaces_the_answer_and_discards_the_draft(session, job, app_):
    q = add(session, job, "Why us?")
    set_drafts(app_, make_draft(q.id))
    first = svc.set_answer(session, job, app_, q.id, "First")
    second = svc.set_answer(session, job, app_, q.id, "Second")
    assert app_.answers == [second] and first.text == "First" and app_.answer_drafts == []
    assert (second.origin, second.bank_id, second.skipped, second.confirmed) == ("user", None, False, False)


def test_unknown_category_questions_cannot_be_answered_until_confirmed(session, job, app_):
    q = add(session, job, "Are you willing to relocate?")
    with pytest.raises(AnswerError, match="Confirm the question's category"):
        svc.set_answer(session, job, app_, q.id, "Yes")
    svc.set_category(session, job, app_, q.id, "sensitive")
    assert svc.set_answer(session, job, app_, q.id, "Yes").text == "Yes"


def test_bank_origin_needs_an_entry_and_never_applies_to_sensitive_questions(session, job, app_):
    q = add(session, job, "Why us?")
    sensitive = add(session, job, "What is your gender?")
    with pytest.raises(AnswerError, match="no longer exists"):
        svc.set_answer(session, job, app_, q.id, "Text", origin="bank", bank_id=99)
    svc.set_answer(session, job, app_, q.id, "Because", origin="user")
    entry = svc.save_to_bank(session, job, app_, q.id)
    answer = svc.set_answer(session, job, app_, q.id, "Because, edited", origin="bank", bank_id=entry.id)
    assert (answer.origin, answer.bank_id) == ("bank", entry.id)
    with pytest.raises(AnswerError, match="never answered from the answer bank"):
        svc.set_answer(session, job, app_, sensitive.id, "x", origin="bank", bank_id=entry.id)
    with pytest.raises(AnswerError):
        svc.set_answer(session, job, app_, q.id, "x", origin="ai_draft")


def test_confirm_answer_stores_the_current_profile_value(session, job, app_, candidate, profile):
    email = add(session, job, "What is your email?")
    auth = add(session, job, "Are you authorized to work in the US?")
    answer = confirm(session, job, email.id, candidate)
    assert (answer.text, answer.origin, answer.confirmed) == ("jordan@example.com", "profile", True)
    assert confirm(session, job, auth.id, candidate).text == "Yes"


def test_confirm_answer_refuses_missing_values_and_other_categories(session, job, app_, candidate, profile):
    portfolio = add(session, job, "Portfolio link")
    openq = add(session, job, "Why us?")
    with pytest.raises(AnswerError, match="no value"):
        confirm(session, job, portfolio.id, candidate)
    with pytest.raises(AnswerError, match="nothing to confirm"):
        confirm(session, job, openq.id, candidate)
    assert app_.answers == []


# ---------------------------------------------------------------- drafts

def test_accept_draft_makes_an_ai_draft_answer(session, job, app_, candidate, profile):
    q = add(session, job, "Why us?", limit=100)
    set_drafts(app_, make_draft(q.id))
    answer = accept(session, job, q.id, candidate)
    assert (answer.origin, answer.sources, answer.text) == ("ai_draft", ["exp-1-b1"], "Built a Flask REST API in Python")
    assert app_.answers == [answer] and app_.answer_drafts == []
    with pytest.raises(AnswerError, match="no draft"):
        accept(session, job, q.id, candidate)


def test_accept_draft_revalidates_against_the_current_profile_and_limit(session, job, app_, candidate):
    q = add(session, job, "Why us?", limit=200)
    set_drafts(app_, make_draft(q.id))
    edited = candidate.profile.model_dump()
    edited["experience"][0]["bullets"][0] = {"id": "exp-1-b1", "text": "Built internal dashboards"}
    profile_service.save(session, edited)
    with pytest.raises(AnswerError, match="no longer passes validation") as exc:
        accept(session, job, q.id, candidate)
    assert exc.value.details
    assert app_.answers == [] and len(app_.answer_drafts) == 1
    # a source that was deleted from the profile is also rejected
    set_drafts(app_, make_draft(q.id, sources=("exp-9-b9",)))
    with pytest.raises(AnswerError):
        accept(session, job, q.id, candidate)


def test_accept_draft_rechecks_the_length_limit(session, job, app_, candidate, profile):
    q = add(session, job, "Why us?", limit=10)
    set_drafts(app_, make_draft(q.id))
    with pytest.raises(AnswerError) as exc:
        accept(session, job, q.id, candidate)
    assert any("exceeds the limit" in d for d in exc.value.details)


def test_accept_draft_refuses_fabricated_claims(session, job, app_, candidate, profile):
    q = add(session, job, "Why us?")
    set_drafts(app_, make_draft(q.id, text="Built a Flask API with 10 years of Java"))
    with pytest.raises(AnswerError) as exc:
        accept(session, job, q.id, candidate)
    assert any("numbers/years" in d for d in exc.value.details) and any("Java" in d for d in exc.value.details)


def test_discard_draft(session, job, app_):
    q = add(session, job, "Why us?")
    with pytest.raises(AnswerError, match="no draft"):
        svc.discard_draft(session, job, app_, q.id)
    set_drafts(app_, make_draft(q.id))
    svc.discard_draft(session, job, app_, q.id)
    assert app_.answer_drafts == []


# ---------------------------------------------------------------- approval

def test_every_content_mutation_clears_approval_but_storing_drafts_does_not(session, job, app_, candidate, profile):
    q = add(session, job, "Why us?")
    optional = add(session, job, "Anything else?", required=False)
    email = add(session, job, "Email?")
    open2 = add(session, job, "Describe a challenge")

    def check(action):
        approve(app_)
        session.commit()
        action()
        session.refresh(app_)
        assert app_.approval is None and app_.review_state == "draft", action

    check(lambda: add(session, job, "Another question about why?"))
    check(lambda: svc.set_answer(session, job, app_, q.id, "Because"))
    check(lambda: svc.skip_answer(session, job, app_, optional.id))
    check(lambda: confirm(session, job, email.id, candidate))
    set_drafts(app_, make_draft(open2.id))
    check(lambda: accept(session, job, open2.id, candidate))
    check(lambda: svc.set_category(session, job, app_, q.id, "sensitive"))
    check(lambda: svc.remove_question(session, job, app_, optional.id))

    approve(app_)
    session.commit()
    svc.replace_drafts(app_, [make_draft(q.id)])
    session.commit()
    assert app_.approval is not None and app_.review_state == "approved"
    svc.discard_draft(session, job, app_, q.id)
    assert app_.approval is not None and app_.review_state == "approved"
    svc.save_to_bank(session, job, app_, open2.id)
    assert app_.approval is not None


def test_failed_mutations_leave_approval_alone(session, job, app_):
    q = add(session, job, "Why us?", limit=5)
    approve(app_)
    session.commit()
    with pytest.raises(AnswerError):
        svc.set_answer(session, job, app_, q.id, "far too long for this")
    with pytest.raises(AnswerError):
        svc.skip_answer(session, job, app_, q.id)
    with pytest.raises(AnswerError):
        add(session, job, "")
    assert app_.approval is not None and app_.review_state == "approved"


def test_replace_drafts_only_replaces_the_targeted_questions(session, job, app_):
    a, b = add(session, job, "Why us?"), add(session, job, "Describe a project")
    svc.replace_drafts(app_, [make_draft(a.id, "Old a"), make_draft(b.id, "Old b")])
    svc.replace_drafts(app_, [make_draft(a.id, "New a")])
    session.commit()
    assert {d.question_id: d.text for d in app_.answer_drafts} == {a.id: "New a", b.id: "Old b"}


# ---------------------------------------------------------------- resolution

def labels(job, app_, profile):
    return {r.question.id: (r.label, r.resolved, r.text) for r in svc.resolve_all(job, app_, profile)}


def test_resolution_labels(session, job, app_, candidate, profile):
    email = add(session, job, "Email?")
    portfolio = add(session, job, "Portfolio?")
    auth = add(session, job, "Are you authorized to work in the US?")
    gender = add(session, job, "What is your gender?")
    why = add(session, job, "Why us?")
    project = add(session, job, "Describe a project")
    unknown = add(session, job, "Are you willing to relocate?")
    optional = add(session, job, "Anything else?", required=False)

    state = labels(job, app_, profile)
    assert state[email.id] == ("From profile", True, "jordan@example.com")  # filled without confirmation
    assert state[portfolio.id] == ("Missing information", False, "")
    assert state[auth.id] == ("User input required", False, "Yes")  # shows the value, needs confirmation
    assert state[gender.id] == ("User input required", False, "")
    assert state[why.id] == ("Missing information", False, "")
    assert state[unknown.id] == ("Confirm category", False, "")

    set_drafts(app_, make_draft(why.id))
    assert labels(job, app_, profile)[why.id] == ("AI draft (pending review)", False, "Built a Flask REST API in Python")
    accept(session, job, why.id, candidate)
    svc.set_answer(session, job, app_, project.id, "A bot")
    svc.set_answer(session, job, app_, gender.id, "Prefer not to say")
    svc.skip_answer(session, job, app_, optional.id)
    confirm(session, job, auth.id, candidate)
    svc.set_answer(session, job, app_, portfolio.id, "example.com/me")
    state = labels(job, app_, profile)
    assert state[why.id][:2] == ("AI draft", True)
    assert state[project.id] == ("User answer", True, "A bot")
    assert state[gender.id] == ("User answer", True, "Prefer not to say")
    assert state[optional.id] == ("Skipped (optional)", True, "")
    assert state[auth.id] == ("From profile (confirmed)", True, "Yes")
    assert state[portfolio.id] == ("User answer", True, "example.com/me")  # a typed answer overrides
    resolved = {r.question.id: r for r in svc.resolve_all(job, app_, profile)}
    assert resolved[why.id].origin == "ai_draft" and resolved[email.id].origin == "profile"
    assert resolved[optional.id].skipped and resolved[unknown.id].origin is None


def test_sensitive_factual_confirmation_goes_stale_when_the_profile_changes(session, job, app_, candidate, profile):
    q = add(session, job, "Will you require visa sponsorship?")
    confirm(session, job, q.id, candidate)
    assert labels(job, app_, profile)[q.id] == ("From profile (confirmed)", True, "No")
    changed = profile.model_copy(update={"authorization": [Authorization(country="US", authorized=True, requires_sponsorship=True)]})
    assert labels(job, app_, changed)[q.id] == ("User input required", False, "Yes")
    gone = profile.model_copy(update={"authorization": []})
    assert labels(job, app_, gone)[q.id] == ("User input required", False, "")
    profile_service.save(session, changed.model_dump())
    confirm(session, job, q.id, candidate)
    assert labels(job, app_, candidate.profile)[q.id] == ("From profile (confirmed)", True, "Yes")


def test_factual_answers_follow_the_profile_without_reconfirming(session, job, app_, candidate, profile):
    q = add(session, job, "What is your phone number?")
    changed = profile.model_copy(update={"contact": profile.contact.model_copy(update={"phone": "+1 555 0111"})})
    assert labels(job, app_, changed)[q.id] == ("From profile", True, "+1 555 0111")
    removed = profile.model_copy(update={"contact": profile.contact.model_copy(update={"phone": ""})})
    assert labels(job, app_, removed)[q.id] == ("Missing information", False, "")


def test_open_answers_ignore_the_profile_and_drafts_never_resolve(session, job, app_, candidate, profile):
    q = add(session, job, "Why us?")
    set_drafts(app_, make_draft(q.id))
    result = svc.resolve_all(job, app_, profile)[0]
    assert not result.resolved and result.draft is not None and result.origin is None


def test_draft_targets_are_open_unanswered_questions(session, job, app_):
    a = add(session, job, "Why us?")
    b = add(session, job, "Describe a project")
    c = add(session, job, "Anything else?", required=False)
    add(session, job, "What is your gender?")
    add(session, job, "Are you willing to relocate?")
    add(session, job, "Email?")
    svc.set_answer(session, job, app_, b.id, "A bot")
    svc.skip_answer(session, job, app_, c.id)
    assert [q.id for q in svc.draft_targets(job, app_)] == [a.id]


# ---------------------------------------------------------------- answer bank

def test_sensitive_answers_never_enter_the_bank(session, job, app_, candidate, profile):
    sensitive = add(session, job, "What is your gender?")
    factual_sensitive = add(session, job, "Are you authorized to work in the US?")
    overridden = add(session, job, "Why us?", category="sensitive")
    svc.set_answer(session, job, app_, sensitive.id, "Prefer not to say")
    confirm(session, job, factual_sensitive.id, candidate)
    svc.set_answer(session, job, app_, overridden.id, "Because")
    for q in (sensitive, factual_sensitive, overridden):
        with pytest.raises(AnswerError, match="never saved"):
            svc.save_to_bank(session, job, app_, q.id)
    assert svc.bank_entries(session) == []


def test_a_question_detected_sensitive_stays_out_of_the_bank_even_if_recategorized(session, job, app_):
    q = add(session, job, "Are you authorized to work in the US?")
    svc.set_category(session, job, app_, q.id, "sensitive")
    svc.set_answer(session, job, app_, q.id, "Yes")
    with pytest.raises(AnswerError):
        svc.save_to_bank(session, job, app_, q.id)


def test_only_accepted_non_skipped_answers_can_be_saved(session, job, app_, candidate, profile):
    open_q = add(session, job, "Why us?")
    optional = add(session, job, "Anything else?", required=False)
    email = add(session, job, "Email?")
    with pytest.raises(AnswerError, match="no accepted answer"):
        svc.save_to_bank(session, job, app_, open_q.id)
    svc.skip_answer(session, job, app_, optional.id)
    with pytest.raises(AnswerError, match="no accepted answer"):
        svc.save_to_bank(session, job, app_, optional.id)
    set_drafts(app_, make_draft(open_q.id))
    with pytest.raises(AnswerError, match="no accepted answer"):  # a pending draft is not accepted
        svc.save_to_bank(session, job, app_, open_q.id)
    confirm(session, job, email.id, candidate)
    with pytest.raises(AnswerError, match="from your profile"):
        svc.save_to_bank(session, job, app_, email.id)


def test_save_to_bank_records_the_answer_and_deduplicates(session, job, app_, candidate, profile):
    q = add(session, job, "Why do you want to work here?")
    set_drafts(app_, make_draft(q.id))
    accept(session, job, q.id, candidate)
    entry = svc.save_to_bank(session, job, app_, q.id)
    assert (entry.question, entry.answer, entry.category, entry.sources, entry.source_job_id) == (
        "Why do you want to work here?", "Built a Flask REST API in Python", "open", ["exp-1-b1"], job.id)
    assert svc.save_to_bank(session, job, app_, q.id).id == entry.id
    assert len(svc.bank_entries(session)) == 1
    svc.set_answer(session, job, app_, q.id, "A different answer")
    assert svc.save_to_bank(session, job, app_, q.id).id != entry.id


def seed(session, question, answer="An answer", category="open"):
    entry = AnswerBankEntry(question=question, answer=answer, category=category, sources=[], created_at=utcnow(), updated_at=utcnow())
    session.add(entry)
    session.commit()
    return entry


def test_bank_suggestions_rank_by_word_overlap(session):
    exact = seed(session, "Why do you want to work at our company?")
    close = seed(session, "Why do you want to join our company and team?")
    far = seed(session, "Describe a time you led a team through a difficult project")
    unrelated = seed(session, "What is your favourite programming language?")
    found = svc.bank_suggestions(session, "Why do you want to work at this company?")
    assert [s.entry.id for s in found] == [exact.id, close.id]
    assert found[0].score > found[1].score >= svc.SIMILARITY_THRESHOLD
    assert far.id not in {s.entry.id for s in found} and unrelated.id not in {s.entry.id for s in found}
    assert svc.bank_suggestions(session, "Why do you want to work at this company?", limit=1)[0].entry.id == exact.id
    assert svc.bank_suggestions(session, "Completely different subject matter") == []


def test_bank_suggestions_ignore_stopwords_and_empty_questions(session):
    seed(session, "What is the best way to do it?")
    assert svc.bank_suggestions(session, "How are you and the team?") == []  # only stopwords overlap
    assert svc.bank_suggestions(session, "") == []
    assert svc.similarity("", "anything") == 0.0
    assert svc.similarity("Python backend", "python BACKEND") == 1.0


def test_bank_suggestions_never_include_sensitive_entries(session):
    seed(session, "What is your gender identity?", category="sensitive")
    seed(session, "Are you authorized to work in the US?", category="sensitive_factual")
    # An entry mislabelled as open is still excluded when its own question reads as sensitive
    seed(session, "What is your expected salary range?", category="open")
    safe = seed(session, "Why do you want this internship role?")
    assert [s.entry.id for s in svc.bank_suggestions(session, "What is your gender identity?")] == []
    assert [s.entry.id for s in svc.bank_suggestions(session, "Authorized to work in the US?")] == []
    assert [s.entry.id for s in svc.bank_suggestions(session, "What is your expected salary range?")] == []
    assert [s.entry.id for s in svc.bank_suggestions(session, "Why do you want this internship?")] == [safe.id]


def test_bank_entries_newest_first_and_delete(session):
    first, second = seed(session, "Why us?"), seed(session, "Why this team?")
    assert [e.id for e in svc.bank_entries(session)] == [second.id, first.id]
    svc.delete_bank_entry(session, first.id)
    assert [e.id for e in svc.bank_entries(session)] == [second.id]
    with pytest.raises(AnswerError, match="no longer exists"):
        svc.delete_bank_entry(session, first.id)
