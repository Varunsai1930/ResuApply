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
