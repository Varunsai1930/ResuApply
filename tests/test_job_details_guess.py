"""Title, company and location filled in from a pasted posting, for the user to check before saving."""

from __future__ import annotations

import pytest

from app.models import Job
from app.services.jobs import LIMITS, guess_details

POSTING = """Backend Engineering Intern
Example Corp · Austin, TX (hybrid)

About the role
You will build Python services that power our analytics platform.
"""


@pytest.mark.parametrize("posting, expected", [
    (POSTING, {"title": "Backend Engineering Intern", "company": "Example Corp", "location": "Austin, TX (hybrid)"}),
    ("Platform Intern | Demo Corp | Remote\nWe build things.", {"title": "Platform Intern", "company": "Demo Corp", "location": "Remote"}),
    ("Data Intern at Sample Labs\nAbout us", {"title": "Data Intern", "company": "Sample Labs"}),
    ("Job title: Data Intern\nCompany: Sample Labs\nLocation: Remote\nMore.", {"title": "Data Intern", "company": "Sample Labs", "location": "Remote"}),
    ("Back-end Developer - Intern\nExample Health", {"title": "Back-end Developer - Intern", "company": "Example Health"}),
    ("About the role\nWe need Python and SQL.", {}),  # a heading is not a title
    ("We are hiring a backend intern to build services that power our platform for thousands of users every day.", {}),
    ("Backend Intern\nYou will build Python services.", {"title": "Backend Intern"}),  # a sentence is not a company
    ("", {}),
    # A labelled title: the unlabelled lines aren't read as the company.
    ("Backend Intern\nJob title: Backend Intern\nLocation: Remote", {"title": "Backend Intern", "location": "Remote"}),
    ("Title: Data Intern\nFull-time, hybrid\nAbout us", {"title": "Data Intern"}),
    ("Title: Data Intern\nCompany: Sample Labs\nAustin, TX", {"title": "Data Intern", "company": "Sample Labs"}),
])
def test_guess_details(posting, expected):
    assert guess_details(posting) == expected


def test_only_the_first_lines_are_read_and_values_fit_the_limits():
    assert "company" not in guess_details("\n".join(["Intro."] * 8 + ["Company: Late Corp"]))
    assert len(guess_details("T" * 100 + "\nCompany: " + "C" * 500)["company"]) == LIMITS["company"]


def jobs_saved(client) -> int:
    with client.app.state.session_factory() as session:
        return session.query(Job).count()


def test_empty_details_are_filled_in_for_the_user_to_check(client):
    response = client.post("/jobs", data={"title": "", "company": "", "description": POSTING}, follow_redirects=False)
    assert response.status_code == 200 and jobs_saved(client) == 0  # nothing saved yet
    page = response.text
    assert "Check these before saving:" in page and "job title, company, location" in page
    assert 'name="title" value="Backend Engineering Intern" required class="guessed"' in page
    assert 'value="Example Corp"' in page and 'value="Austin, TX (hybrid)"' in page
    assert page.count("Filled in from the posting. Check it.") == 3
    saved = client.post("/jobs", data={"title": "Backend Engineering Intern", "company": "Example Corp",
                                       "location": "Austin, TX (hybrid)", "description": POSTING}, follow_redirects=False)
    assert saved.status_code == 303 and jobs_saved(client) == 1


def test_what_the_user_typed_is_never_replaced(client):
    response = client.post("/jobs", data={"title": "My own title", "company": "", "location": "",
                                          "description": POSTING})
    page = response.text
    assert 'value="My own title"' in page and "Backend Engineering Intern\"" not in page
    assert "company, location" in page and "job title" not in page.split("Check these before saving:")[1][:200]


def test_complete_details_save_without_guessing(client):
    response = client.post("/jobs", data={"title": "Intern", "company": "Corp", "description": POSTING},
                           follow_redirects=False)
    assert response.status_code == 303
    with client.app.state.session_factory() as session:
        assert session.query(Job).one().location == ""  # an empty optional field stays empty


def test_nothing_to_guess_reports_the_missing_fields(client):
    response = client.post("/jobs", data={"title": "", "company": "", "description": "About the role\nPython."})
    assert response.status_code == 422 and "Job title is required." in response.text
    # A second submit after a guess that left the company out reports it instead of guessing again.
    again = client.post("/jobs", data={"title": "Backend Intern", "company": "",
                                       "description": "Backend Intern\nYou will build Python services."})
    assert again.status_code == 422 and "Company is required." in again.text


def test_guessed_text_is_escaped(client):
    response = client.post("/jobs", data={"title": "", "company": "", "description": '<b>Intern</b>\n"Evil" Corp'})
    assert "<b>Intern" not in response.text and 'value="&lt;b&gt;Intern&lt;/b&gt;"' in response.text
    assert 'value="&#34;Evil&#34; Corp"' in response.text
