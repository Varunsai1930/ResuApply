"""The jobs board: columns by tracking status, each card's next action, the summary strip and search."""

from __future__ import annotations

import re
from datetime import date

from app.services import board as board_service
from app.services import jobs as job_service
from app.services.profile import get_candidate
from app.services.status import Status
from tests.conftest import SAMPLE_JOB
from tests.test_assessment_routes import setup as job_with_requirements
from tests.test_questions_routes import approve_package, complete_package
from tests.test_resume_routes import accept, manual_proposal
from tests.test_routes import board_column

TODAY = date(2026, 10, 11)


def new_job(client, **fields) -> int:
    created = client.post("/jobs", data=SAMPLE_JOB | fields, follow_redirects=False)
    return int(re.match(r"/jobs/(\d+)", created.headers["location"]).group(1))


def set_status(client, job_id, status, on):
    response = client.post(f"/jobs/{job_id}/status", data={"status": status, "on": on}, follow_redirects=False)
    assert response.status_code == 303, response.text


def built(client, query=""):
    with client.app.state.session_factory() as session:
        jobs = job_service.list_all(session)
        return board_service.board(jobs, get_candidate(session), TODAY, query)


def cards_by_column(board):
    return {col.key: [c.job.title for c in col.cards] for col in board.columns}


# ---------------------------------------------------------------- columns and cards

def test_each_status_has_one_column(client):
    statuses = ["saved", "applied", "assessment", "interview", "offer", "rejected", "withdrawn"]
    for status in statuses:
        job_id = new_job(client, title=f"{status.title()} Intern")
        if status != "saved":
            set_status(client, job_id, status, "2026-10-01")
    assert cards_by_column(built(client)) == {
        "saved": ["Saved Intern"],
        "applied": ["Assessment Intern", "Applied Intern"],  # an assessment still waits on the employer
        "interview": ["Interview Intern"],
        "offer": ["Offer Intern"],
        "closed": ["Withdrawn Intern", "Rejected Intern"],
    }


def test_a_saved_card_shows_the_next_action_and_its_step(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    card = built(client).columns[0].cards[0]
    assert (card.meaning.status, card.meaning.note, card.step.number) == (Status.NEEDS_YOU, "Prepare a resume", 2)
    page = client.get("/jobs").text
    saved = board_column(page, "saved")
    assert f'href="/jobs/{job_id}"' in saved
    assert "Needs you: Prepare a resume" in saved and "Step 2 of 5 · Resume" in saved
    assert 'class="st st-needs_you"' in saved and "Package:" in saved


def test_without_a_profile_the_card_asks_for_one(client):
    new_job(client)
    card = built(client).columns[0].cards[0]
    assert (card.meaning.status, card.meaning.note) == (Status.NEEDS_YOU, "Create your profile")


def test_an_approved_job_is_ready_to_submit(client, sample_profile):
    job_id = job_with_requirements(client, sample_profile)
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    complete_package(client, job_id)
    assert approve_package(client, job_id).status_code == 303
    board = built(client)
    card = board.columns[0].cards[0]
    assert (card.meaning.status, card.meaning.note, card.step.number) == (Status.NEEDS_YOU, "Record that you applied", 5)
    assert board.summary.ready == 1


def test_cards_past_saved_show_where_the_job_stands(client):
    applied = new_job(client, title="Applied Intern")
    set_status(client, applied, "applied", "2026-10-02")
    offer = new_job(client, title="Offer Intern")
    set_status(client, offer, "offer", "2026-10-05")
    closed = new_job(client, title="Closed Intern")
    set_status(client, closed, "rejected", "2026-09-20")
    client.post(f"/jobs/{closed}/notes", data={"text": "Ask for feedback"})
    meanings = {c.job.title: (c.meaning.status, c.meaning.note, c.step) for col in built(client).columns for c in col.cards}
    assert meanings["Applied Intern"] == (Status.INFO, "Applied 2026-10-02, 9 days ago", None)
    assert meanings["Offer Intern"] == (Status.DONE, "Offer on 2026-10-05", None)
    assert meanings["Closed Intern"] == (Status.INFO, "Rejected on 2026-09-20, notes kept", None)
    applied_column = board_column(client.get("/jobs").text, "applied")
    assert '<span class="visually-hidden">Info: </span>Applied 2026-10-02' in applied_column  # a fact, not an action


def test_empty_columns_say_what_belongs_there(client):
    new_job(client)
    page = client.get("/jobs").text
    assert "Offers appear here when you record one." in board_column(page, "offer")
    assert 'class="job-card"' not in board_column(page, "offer")


# ---------------------------------------------------------------- summary

def test_summary_counts_every_job(client):
    for title, status, on in (("A", "applied", "2026-10-03"), ("B", "interview", "2026-10-06"),
                              ("C", "rejected", "2026-10-07"), ("D", "applied", "2026-09-15"), ("E", None, None)):
        job_id = new_job(client, title=title)
        if title in "BC":  # applied first, then heard back
            set_status(client, job_id, "applied", "2026-10-01")
        if status:
            set_status(client, job_id, status, on)
    s = built(client).summary
    assert (s.active, s.applied_this_month, s.heard_back, s.applied, s.ready) == (4, 3, 2, 4, 0)
    page = client.get("/jobs").text
    strip = page[page.index('aria-label="Summary of all jobs"'):page.index("</section>", page.index('aria-label="Summary'))]
    assert "<span>Heard back</span><strong>" in strip


# ---------------------------------------------------------------- search

def test_search_matches_every_word_in_title_company_or_location(client):
    new_job(client, title="Backend Intern", company="Example Corp", location="Austin, TX")
    new_job(client, title="Data Intern", company="Sample Labs", location="Remote")
    assert cards_by_column(built(client, "example austin"))["saved"] == ["Backend Intern"]
    assert cards_by_column(built(client, "  INTERN  "))["saved"] == ["Data Intern", "Backend Intern"]
    assert cards_by_column(built(client, "labs austin"))["saved"] == []
    assert built(client, "labs").summary.active == 2  # the summary always counts every job


def test_search_over_http_shows_the_matches_and_a_way_back(client):
    new_job(client, title="Backend Intern")
    new_job(client, title="Data Intern")
    page = client.get("/jobs", params={"q": "data"}).text
    assert "1 job match “data”" in page and '<a href="/jobs">Clear search</a>' in page
    assert "Data Intern" in board_column(page, "saved") and "Backend Intern" not in board_column(page, "saved")
    assert 'value="data"' in page
    none = client.get("/jobs", params={"q": "nothing here"}).text
    assert "No jobs match" in none and 'class="board"' not in none


def test_search_text_is_escaped_and_cut_to_the_limit(client):
    new_job(client)
    page = client.get("/jobs", params={"q": "<script>alert(1)</script>"}).text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert len(board_service.clean_query("x" * 5000)) == board_service.SEARCH_LIMIT


def test_the_legend_explains_the_four_meanings(client):
    new_job(client)
    page = client.get("/jobs").text
    legend = page[page.index('aria-label="What the statuses mean"'):]
    for text in ("Done · nothing left to do", "Needs you · a decision or an answer",
                 "Blocked · can&#39;t continue until fixed", "Info · worth a look, never blocking"):
        assert text in legend
