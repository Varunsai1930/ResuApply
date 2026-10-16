"""The welcome page, and loading and removing the sample data."""

from __future__ import annotations

import re

from sqlalchemy import inspect, text

from app.db import init_db, make_engine, make_session_factory
from app.models import AnswerBankEntry, Application, Candidate, Job, OutboundApproval
from app.sample_data import SAMPLE_JOB, SAMPLE_PROFILE
from app.services import jobs
from app.services.profile import get_candidate
from tests.synthetic import DEMO_JOB
from tests.test_jobs_board import new_job
from tests.test_questions_routes import add, post, qid_of
from tests.test_resume_routes import accept, approval, manual_proposal
from tests.test_routes import profile_form, review_and_save


def load(client):
    response = client.post("/sample", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/jobs?msg=sample_loaded"


def counts(client):
    with client.app.state.session_factory() as session:
        return {model.__name__: session.query(model).count()
                for model in (Candidate, Job, Application, AnswerBankEntry, OutboundApproval)}


# ---------------------------------------------------------------- the welcome page

def test_a_fresh_install_opens_the_welcome_page_and_later_the_board(client, sample_profile):
    assert client.get("/", follow_redirects=False).headers["location"] == "/welcome"
    page = client.get("/welcome").text
    heading = re.search(r'<h1[^>]*id="welcome-title"[^>]*>(.*?)</h1>', page, re.S).group(1)
    assert " ".join(re.sub(r"<[^>]+>", " ", heading).split()) == "Your experience. Your next opportunity."
    assert 'action="/sample"' in page
    assert 'href="/profile/edit">Open the profile form' in page
    new_job(client)  # a job, even without a profile, means it's no longer a fresh install
    assert client.get("/", follow_redirects=False).headers["location"] == "/jobs"
    assert client.head("/", follow_redirects=False).status_code == 303


def test_the_jobs_page_offers_the_sample_data_before_a_profile_exists(client):
    assert 'href="/welcome">or try it with sample data' in client.get("/jobs").text


# ---------------------------------------------------------------- loading

def test_loading_adds_a_fictional_profile_and_a_job_marked_sample(client):
    load(client)
    with client.app.state.session_factory() as session:
        candidate = get_candidate(session)
        assert candidate.sample and candidate.profile.contact.name == SAMPLE_PROFILE["contact"]["name"]
        job = session.query(Job).one()
        assert job.sample and job.title == SAMPLE_JOB["title"] and len(job.requirements) == 7
        assert "IMPORTANT SYSTEM NOTE" not in job.description  # the tests' injection line isn't sample data
    board = client.get("/jobs?msg=sample_loaded").text
    assert "Sample data loaded" in board and "You're exploring sample data." in board
    assert '<span class="tag">Sample</span>' in board
    assert "Needs you: Prepare a resume" in board  # requirements already reviewed: the walkthrough starts at step 2
    assert "Sample job: fictional" in client.get(f"/jobs/{job.id}").text
    assert '<span class="tag">Sample</span>' in client.get("/profile").text


def test_loading_never_touches_a_real_profile(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    before = counts(client)
    refused = client.post("/sample")
    assert refused.status_code == 409 and "only be loaded before you create a profile" in refused.text
    assert counts(client) == before
    assert 'action="/sample"' not in client.get("/welcome").text


def test_loading_twice_is_refused(client):
    load(client)
    assert client.post("/sample").status_code == 409
    assert counts(client)["Job"] == 1
    page = client.get("/welcome").text
    assert 'action="/sample"' not in page and "The sample data is loaded." in page


# ---------------------------------------------------------------- removing

def test_removing_takes_away_the_sample_and_keeps_the_users_own_jobs(ai_app_client, sample_profile):
    client = ai_app_client
    load(client)
    with client.app.state.session_factory() as session:
        sample_job = session.query(Job).one().id
    approval(client)  # an AI sharing approval for the sample profile
    assert accept(client, sample_job, manual_proposal(client, sample_job)).status_code == 303
    why = qid_of(add(client, sample_job, "Why do you want this role?"))
    post(client, sample_job, why, "answer", text="I like building Python services.")
    assert post(client, sample_job, why, "bank").status_code == 303
    own = new_job(client, title="My Own Job")
    assert counts(client) == {"Candidate": 1, "Job": 2, "Application": 2, "AnswerBankEntry": 1, "OutboundApproval": 1}

    confirm = client.get("/sample/remove").text
    assert "The fictional profile of Jordan Example" in confirm
    assert "The sample job Backend Engineering Intern at Demo Corp" in confirm
    assert "1 answer saved to the answer bank" in confirm and "My Own Job" not in confirm
    removed = client.post("/sample/remove", follow_redirects=False)
    assert removed.headers["location"] == "/welcome?msg=sample_removed"

    assert counts(client) == {"Candidate": 0, "Job": 1, "Application": 1, "AnswerBankEntry": 0, "OutboundApproval": 0}
    with client.app.state.session_factory() as session:
        kept = session.get(Job, own)
        assert kept.title == "My Own Job" and kept.application.candidate_id is None
    assert client.get(f"/jobs/{own}").status_code == 200
    welcome = client.get("/welcome?msg=sample_removed").text
    assert "Sample data removed." in welcome and 'action="/sample"' in welcome  # can be loaded again
    assert "exploring sample data" not in client.get("/jobs").text


def test_removing_with_nothing_to_remove_changes_nothing(client, sample_profile):
    review_and_save(client, profile_form(sample_profile))
    new_job(client)
    before = counts(client)
    assert "There's no sample data" in client.get("/sample/remove").text
    assert client.post("/sample/remove").status_code == 200
    assert counts(client) == before


def test_an_existing_database_gains_the_sample_columns(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    init_db(engine)
    with make_session_factory(engine)() as session:
        jobs.create(session, jobs.clean_input(**DEMO_JOB))
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN sample"))
        conn.execute(text("ALTER TABLE candidates DROP COLUMN sample"))
        conn.execute(text("DROP TABLE alembic_version"))  # made before migrations
    init_db(engine)
    for table in ("jobs", "candidates"):
        assert "sample" in {c["name"] for c in inspect(engine).get_columns(table)}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT sample FROM jobs")).scalar_one() == 0  # existing jobs are not sample
    engine.dispose()


def test_sample_text_is_escaped_in_the_banner(client):
    load(client)
    with client.app.state.session_factory() as session:
        candidate = get_candidate(session)
        profile = candidate.profile.model_copy(update={"contact": candidate.profile.contact.model_copy(
            update={"name": "<b>Jordan</b>"})})
        candidate.profile = profile
        session.commit()
    page = client.get("/jobs").text
    assert "<b>Jordan" not in page and re.search(r"The profile of &lt;b&gt;Jordan&lt;/b&gt;", page)
