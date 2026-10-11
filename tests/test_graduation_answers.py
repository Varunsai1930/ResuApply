"""Graduation questions answered from the profile's education end date (services/questions.py)."""

from __future__ import annotations

import pytest

from app.services.questions import classify, factual_value


def profile(*ends: str, degrees: tuple[str, ...] = ()) -> dict:
    degrees = degrees or ("B.S.",) * len(ends)
    return {"education": [{"institution": f"School {i}", "degree": degree, "end": end}
                          for i, (end, degree) in enumerate(zip(ends, degrees))]}


@pytest.mark.parametrize("question", [
    "What is your graduation year?", "Graduation year", "Expected graduation year", "Year of graduation",
    "Anticipated graduation year (YYYY)", "What year will you graduate?", "What year do you expect to graduate?",
    "What year did you graduate?",
])
def test_year_questions_are_factual_and_answered_with_the_year(question):
    assert classify(question) == ("factual", "graduation_year")
    assert factual_value("graduation_year", question, profile("2027-05")) == "2027"
    assert factual_value("graduation_year", question, profile("2027")) == "2027"  # a year is precise enough
    assert factual_value("graduation_year", question, profile("2027-05-15")) == "2027"


@pytest.mark.parametrize("question", [
    "Tell us about the year you graduated and what you learned",  # a narrative, not a field request
    "Graduation year of your reference",
])
def test_year_mentions_inside_other_requests_are_not_factual(question):
    assert classify(question)[1] != "graduation_year"


def test_no_end_date_is_left_to_the_user():
    assert factual_value("graduation_year", "Graduation year", profile()) is None
    assert factual_value("graduation_year", "Graduation year", profile("present")) is None


@pytest.mark.parametrize("question", [
    "Expected graduation date", "When did you graduate?", "When will you graduate?", "Graduation month and year",
    "Month and year of graduation", "Anticipated graduation date (Month/Year)", "Graduation date (MM/YYYY)",
])
def test_date_questions_are_factual(question):
    assert classify(question) == ("factual", "graduation_date")


@pytest.mark.parametrize("question, end, answer", [
    ("Expected graduation date", "2027-05", "2027-05"),
    ("Expected graduation date", "2027-05-15", "2027-05"),  # month and year, not the day
    ("Graduation date (MM/YYYY)", "2027-05", "05/2027"),
    ("Graduation date (mm/yy)", "2027-05", "05/27"),
    ("Anticipated graduation date (Month/Year)", "2027-05", "05/2027"),
    ("Graduation date (YYYY-MM)", "2027-05", "2027-05"),
    ("Graduation date (MM-YYYY)", "2027-05", "05-2027"),
    ("Graduation date (MM/DD/YYYY)", "2027-05-15", "05/15/2027"),
])
def test_a_date_question_gets_the_month_and_year_in_the_format_it_asks_for(question, end, answer):
    assert factual_value("graduation_date", question, profile(end)) == answer


@pytest.mark.parametrize("question, end", [
    ("Expected graduation date", "2027"),  # no month
    ("Graduation date (MM/DD/YYYY)", "2027-05"),  # no day
])
def test_a_date_the_profile_doesnt_hold_precisely_enough_is_left_to_the_user(question, end):
    from app.services.questions import graduation_note

    assert factual_value("graduation_date", question, profile(end)) is None
    assert "asks for more than your profile holds" in graduation_note("graduation_date", question, profile(end))
    assert graduation_note("graduation_date", question, profile("2027-05-15")) is None


# ---------------------------------------------------------------- in the Questions step

from tests.test_questions_routes import add, qid_of, ready_job, row, workspace  # noqa: E402


def test_graduation_questions_are_filled_in_from_the_profile_and_stay_editable(client, sample_profile):
    job_id = ready_job(client, sample_profile)  # the sample profile's degree ends 2099-05
    year, date = qid_of(add(client, job_id, "What is your graduation year?")), qid_of(
        add(client, job_id, "Graduation date (MM/YYYY)"))
    page = workspace(client, job_id)
    assert '<div class="answer-text">2099</div>' in row(page, year) and "From profile" in row(page, year)
    assert '<div class="answer-text">05/2099</div>' in row(page, date)
    typed = client.post(f"/jobs/{job_id}/questions/{year}/answer", data={"text": "2100", "origin": "user"},
                        follow_redirects=False)
    assert typed.status_code == 303
    assert '<div class="answer-text">2100</div>' in row(workspace(client, job_id), year)  # the user's answer stands


def test_a_date_the_profile_holds_only_as_a_year_says_why(client, sample_profile):
    sample_profile["education"][0]["end"] = "2099"
    job_id = ready_job(client, sample_profile)
    qid = qid_of(add(client, job_id, "Expected graduation date"))
    html = row(workspace(client, job_id), qid)
    assert "the end date of your education has no month" in html
    assert "Your profile has no value for this" not in html
    refused = client.post(f"/jobs/{job_id}/questions/{qid}/confirm", data={"confirmation_token": "x"})
    assert "the end date of your education has no month" in refused.text


def test_several_degrees_leave_an_unspecific_question_to_the_user():
    from app.services.questions import graduation_note, profile_gap

    two = profile("2023-05", "2027-05", degrees=("Associate of Science", "B.S."))
    for key, question in (("graduation_date", "Expected graduation date"), ("graduation_year", "Graduation year")):
        assert factual_value(key, question, two) is None
        assert "several degrees" in graduation_note(key, question, two)
        assert profile_gap(key, question, two) is None  # adding a fact wouldn't help: the question must say which


@pytest.mark.parametrize("question, answer", [
    ("Bachelor's degree graduation date", "2027-05"),
    ("Expected graduation date (undergraduate)", "2027-05"),
    ("Associate degree graduation year", "2023"),
])
def test_a_question_that_names_the_degree_is_answered_for_that_degree(question, answer):
    two = profile("2023-05", "2027-05", degrees=("Associate of Science", "Bachelor of Science"))
    category, key = classify(question)
    assert category == "factual" and factual_value(key, question, two) == answer


def test_a_degree_the_profile_doesnt_list_is_left_to_the_user():
    from app.services.questions import graduation_note

    one = profile("2027-05", degrees=("B.S.",))
    assert factual_value("graduation_date", "Master's degree graduation date", one) is None
    assert "a kind of degree your profile doesn't list" in graduation_note(
        "graduation_date", "Master's degree graduation date", one)


def test_degrees_that_give_the_same_answer_are_not_ambiguous():
    double = profile("2027-05", "2027-12", degrees=("B.S.", "B.A."))
    assert factual_value("graduation_year", "Graduation year", double) == "2027"
    assert factual_value("graduation_date", "Graduation date", double) is None  # May or December?


# ---------------------------------------------------------------- questions saved under the older rules

from app.db import utcnow  # noqa: E402
from app.schemas.package import Answer, AnswerDraft, Question  # noqa: E402
from app.services import answers, jobs  # noqa: E402
from app.services import profile as profile_service  # noqa: E402
from tests.conftest import SAMPLE_JOB  # noqa: E402


def saved_job(session, sample_profile, *questions: Question, answers_=(), drafts=()):
    candidate = profile_service.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    job.questions = list(questions)
    job.question_counter = len(questions)
    job.application.answers = list(answers_)
    job.application.answer_drafts = list(drafts)
    session.commit()
    return candidate, job


def legacy(qid: str, text: str, category: str, detected: str | None = None) -> Question:
    """A question as the older rules stored it."""
    return Question(id=qid, text=text, category=category, detected_category=detected or category, added_at=utcnow())


def test_startup_reads_saved_graduation_questions_with_the_current_rules(session, sample_profile):
    candidate, job = saved_job(
        session, sample_profile,
        legacy("q1", "What is your graduation year?", "open"),
        legacy("q2", "Graduation year", "unknown"),
        legacy("q3", "When did you graduate?", "unknown"),
        legacy("q4", "Why do you want this role?", "open"),
        drafts=[AnswerDraft(question_id="q1", text="I graduate in 2099.", sources=["edu-1"], model="test/model",
                            prompt_revision="1", profile_revision=1, job_revision=1, created_at=utcnow())],
    )
    assert answers.upgrade_stored_questions(session) == 3
    stored = {q.id: (q.category, q.detected_category, q.factual_key) for q in job.questions}
    assert stored == {"q1": ("factual", "factual", "graduation_year"), "q2": ("factual", "factual", "graduation_year"),
                      "q3": ("factual", "factual", "graduation_date"), "q4": ("open", "open", None)}
    assert job.application.answer_drafts == []  # an AI draft was made for the old category
    resolved = {r.question.id: (r.text, r.label) for r in answers.resolve_all(job, job.application, candidate.profile)}
    assert resolved["q1"] == ("2099", "From profile") and resolved["q3"] == ("2099-05", "From profile")
    assert answers.upgrade_stored_questions(session) == 0  # once upgraded, nothing changes again


def test_the_upgrade_keeps_what_the_user_wrote_chose_or_skipped(session, sample_profile):
    candidate, job = saved_job(
        session, sample_profile,
        legacy("q1", "What is your graduation year?", "open"),
        legacy("q2", "Graduation year", "open", detected="unknown"),  # the user chose Open-ended
        legacy("q3", "Expected graduation year", "unknown"),
        answers_=[Answer(question_id="q1", text="Class of 2099, December", origin="user", at=utcnow()),
                  Answer(question_id="q2", text="2099", origin="user", at=utcnow()),
                  Answer(question_id="q3", text="", origin="user", skipped=True, at=utcnow())],
    )
    job.questions = [q.model_copy(update={"required": q.id != "q3"}) for q in job.questions]
    session.commit()
    assert answers.upgrade_stored_questions(session) == 2  # q2 keeps the category the user chose
    stored = {q.id: q.category for q in job.questions}
    assert stored == {"q1": "factual", "q2": "open", "q3": "factual"}
    kept = {a.question_id: (a.text, a.skipped) for a in job.application.answers}
    assert kept == {"q1": ("Class of 2099, December", False), "q2": ("2099", False), "q3": ("", True)}
    resolved = {r.question.id: r.text for r in answers.resolve_all(job, job.application, candidate.profile)}
    assert resolved["q1"] == "Class of 2099, December"  # never overwritten by the profile value


def test_saved_graduation_questions_are_upgraded_when_the_app_starts(settings, sample_profile):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import BASE_URL

    with TestClient(create_app(settings), base_url=BASE_URL) as first:
        with first.app.state.session_factory() as session:
            _, job = saved_job(session, sample_profile, legacy("q1", "What is your graduation year?", "open"))
            job_id = job.id
    with TestClient(create_app(settings), base_url=BASE_URL) as restarted:
        with restarted.app.state.session_factory() as session:
            (stored,) = jobs.get(session, job_id).questions
            assert (stored.category, stored.factual_key) == ("factual", "graduation_year")


@pytest.mark.parametrize("question, end, answer", [
    ("Graduation date (YYYY)", "2027-05-15", "2027"),
    ("Graduation date (YYYY)", "2027", "2027"),  # only the year is asked for: a year-only profile answers it
    ("Graduation date (YYYY-MM-DD)", "2027-05-15", "2027-05-15"),
    ("Graduation date (DD-MM-YYYY)", "2027-05-15", "15-05-2027"),
    ("Graduation date (MM-DD-YYYY)", "2027-05-15", "05-15-2027"),
    ("Graduation date (DD/MM/YY)", "2027-05-15", "15/05/27"),
    ("Graduation date (MM/DD/YY)", "2027-05-15", "05/15/27"),
    ("Graduation date (M/YYYY)", "2027-05", "5/2027"),
    ("Graduation date (DD.MM.YYYY)", "2027-05-15", "15.05.2027"),
    ("Graduation year (YY)", "2027-05", "27"),
])
def test_every_format_hint_the_rules_accept_is_followed(question, end, answer):
    assert classify(question)[0] == "factual"
    assert factual_value(classify(question)[1], question, profile(end)) == answer


@pytest.mark.parametrize("question, end", [
    ("Graduation date (YYYY-MM-DD)", "2027-05"),
    ("Graduation date (DD/MM/YY)", "2027-05"),
])
def test_a_format_asking_for_a_day_the_profile_lacks_is_left_to_the_user(question, end):
    from app.services.questions import graduation_note

    assert factual_value("graduation_date", question, profile(end)) is None
    assert "has no day" in graduation_note("graduation_date", question, profile(end))


def test_a_format_resuapply_cant_fill_is_left_to_the_user():
    from app.services.questions import graduation_note

    question = "Graduation date (MMM-YYYY)"
    assert classify(question) == ("factual", "graduation_date")
    assert factual_value("graduation_date", question, profile("2027-05")) is None
    assert "a format ResuApply can't fill in" in graduation_note("graduation_date", question, profile("2027-05"))


@pytest.mark.parametrize("degree", ["BBA", "B.E.", "B.Com", "BFA", "LLB", "B.Arch", "BSc", "B.Tech", "", "Diploma"])
def test_a_bachelors_question_is_answered_from_the_only_degree_unless_it_is_another_level(degree):
    assert factual_value("graduation_date", "Bachelor's degree graduation date",
                         profile("2027-05", degrees=(degree,))) == "2027-05"


@pytest.mark.parametrize("degree", ["MFA", "M.Ed.", "LLM", "MPH", "M.Tech"])
def test_masters_abbreviations_are_masters_degrees(degree):
    one = profile("2027-05", degrees=(degree,))
    assert factual_value("graduation_date", "Master's degree graduation date", one) == "2027-05"
    assert factual_value("graduation_date", "Bachelor's degree graduation date", one) is None  # another level
