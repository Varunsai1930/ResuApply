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
