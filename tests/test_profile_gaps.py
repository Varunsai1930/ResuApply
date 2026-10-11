"""What the questions in the user's jobs ask for that the profile lacks (services/profile_gaps.py)."""

from __future__ import annotations

import copy

import pytest

from app.services import profile as profile_service
from app.services.questions import profile_gap
from tests.synthetic import SAMPLE_PROFILE
from tests.test_jobs_board import new_job, set_status
from tests.test_questions_routes import add
from tests.test_routes import profile_form, review_and_save

PORTFOLIO_Q = "Portfolio website"
CANADA_Q = "Are you legally authorized to work in Canada?"


def profile(**changes):
    data = copy.deepcopy(SAMPLE_PROFILE) | changes
    return profile_service.normalize(data).profile


@pytest.mark.parametrize("key, question, expected", [
    ("portfolio", PORTFOLIO_Q, "portfolio"),  # the sample profile has no portfolio link
    ("email", "Email address", None),  # the profile has it
    ("first_name", "First name", None),  # always typed by the user, never a profile gap
    ("authorization", CANADA_Q, "authorization:CA"),
    ("authorization", "Are you legally authorized to work in the United States?", None),
    ("sponsorship", "Will you require visa sponsorship to work in Canada?", "sponsorship:CA"),
    ("authorization", "Are you not authorized to work in Canada?", None),  # negated: always the user's answer
    ("authorization", "Are you authorized to work in the US or Canada?", None),  # unclear country
])
def test_profile_gap_names_only_facts_the_profile_could_hold(key, question, expected):
    assert profile_gap(key, question, profile()) == expected


def test_a_question_naming_no_country_uses_the_only_record_or_none():
    question = "Are you legally authorized to work?"
    assert profile_gap("authorization", question, profile()) is None  # the one US record answers it
    unknown = [{"country": "US", "authorized": None, "requires_sponsorship": None}]
    assert profile_gap("authorization", question, profile(authorization=unknown)) == "authorization:US"
    assert profile_gap("authorization", question, profile(authorization=[])) == "authorization"
    two = [{"country": "US", "authorized": None}, {"country": "CA", "authorized": None}]
    assert profile_gap("authorization", question, profile(authorization=two)) is None  # which one is unclear


def setup_profile(client, sample_profile):
    _, saved = review_and_save(client, profile_form(sample_profile))
    assert saved.status_code == 303


def gaps_card(client) -> str:
    page = client.get("/profile").text
    start = page.index('aria-labelledby="gaps-title"')
    return page[start:page.index("</section>", start)]


def test_the_profile_lists_missing_facts_with_the_jobs_asking(client, sample_profile):
    setup_profile(client, sample_profile)
    first = new_job(client, title="Backend Intern")
    second = new_job(client, title="Data Intern")
    for job_id in (first, second):
        add(client, job_id, PORTFOLIO_Q)
    add(client, second, CANADA_Q)
    add(client, second, "Email address")
    card = gaps_card(client)
    assert card.index("Portfolio") < card.index("Work authorization in Canada")  # most asked first
    assert "Asked by 2 jobs:" in card and "Asked by 1 job:" in card
    assert f'href="/jobs/{first}?step=questions#questions">Backend Intern at' in card
    assert 'href="/profile/edit?section=contact">Add it<span class="visually-hidden">: Portfolio' in card
    assert 'href="/profile/edit?section=authorization">Add it' in card
    assert "Email" not in card.replace("Email address", "")


def test_closed_jobs_no_longer_ask(client, sample_profile):
    setup_profile(client, sample_profile)
    job_id = new_job(client)
    add(client, job_id, PORTFOLIO_Q)
    set_status(client, job_id, "rejected", "2026-10-01")
    assert "Portfolio" not in gaps_card(client)


def test_a_complete_profile_says_so_and_no_questions_says_what_will_show(client, sample_profile):
    setup_profile(client, sample_profile)
    assert "Once you add questions to a job" in gaps_card(client)
    add(client, new_job(client), "Email address")
    assert "everything your jobs&#39; questions ask for so far is in your profile" in gaps_card(client)


def test_adding_the_fact_clears_the_gap(client, sample_profile):
    setup_profile(client, sample_profile)
    add(client, new_job(client), PORTFOLIO_Q)
    assert "Portfolio" in gaps_card(client)
    sample_profile["contact"]["links"]["portfolio"] = "https://jordan.example"
    review_and_save(client, profile_form(sample_profile))
    assert "Portfolio" not in gaps_card(client)
