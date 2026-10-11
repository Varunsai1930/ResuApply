"""Follow-up reminders: set when recording Applied, due while there's no reply, and the two actions."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import inspect, text

from app.db import init_db, make_engine, make_session_factory
from app.models import Job
from app.services import jobs, tracking
from app.services.profile import get_candidate
from app.services.status import Status
from app.services.workspace import Step, next_action
from tests.conftest import DEMO_JOB
from tests.test_jobs_board import built, new_job
from tests.test_questions_routes import approve_package, complete_package, page_token, ready_job, workspace

TODAY = date.today()


def days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def apply(client, job_id, on, follow_up="7", **extra):
    data = {"status": "applied", "on": on, "follow_up": follow_up, **extra}
    return client.post(f"/jobs/{job_id}/status", data=data, follow_redirects=False)


def stored(client, job_id):
    with client.app.state.session_factory() as session:
        app_ = session.get(Job, job_id).application
        return app_.follow_up_after, [n.text for n in app_.notes]


# ---------------------------------------------------------------- setting the reminder

@pytest.mark.parametrize("choice, expected", [("7", 7), ("10", 10), ("14", 14), ("", None)])
def test_recording_applied_sets_the_reminder_from_the_applied_date(client, choice, expected):
    job_id = new_job(client)
    assert apply(client, job_id, days_ago(2), choice).status_code == 303
    after, _ = stored(client, job_id)
    assert after == (TODAY - timedelta(days=2) + timedelta(days=expected) if expected else None)


@pytest.mark.parametrize("choice", ["5", "x", "7.0", "٧", "-7"])
def test_a_reminder_off_the_list_is_refused_and_nothing_changes(client, choice):
    job_id = new_job(client)
    response = apply(client, job_id, days_ago(1), choice)
    assert response.status_code == 422 and "Choose when to be reminded" in response.text
    assert 'name="follow_up" class="invalid" aria-invalid="true"' in response.text  # the form is shown again
    with client.app.state.session_factory() as session:
        assert session.get(Job, job_id).application.tracking_status == "saved"


def test_the_choice_only_matters_when_recording_applied(client):
    job_id = new_job(client)
    response = client.post(f"/jobs/{job_id}/status", data={"status": "interview", "on": days_ago(1), "follow_up": "bad"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert stored(client, job_id)[0] is None


def test_recording_applied_with_the_package_sets_the_reminder_too(client, sample_profile):
    job_id = ready_job(client, sample_profile)
    complete_package(client, job_id)
    assert approve_package(client, job_id).status_code == 303
    token = page_token(workspace(client, job_id, "track"), "package_token")
    response = apply(client, job_id, days_ago(0), "14", snapshot="1", package_token=token)
    assert response.headers["location"].endswith("msg=status_recorded#tracking")
    assert stored(client, job_id)[0] == TODAY + timedelta(days=14)


def test_any_reply_clears_the_reminder(client):
    job_id = new_job(client)
    apply(client, job_id, days_ago(10))
    client.post(f"/jobs/{job_id}/status", data={"status": "interview", "on": days_ago(1)})
    assert stored(client, job_id)[0] is None


def test_the_status_form_offers_the_choice_only_for_applied(client):
    job_id = new_job(client)
    page = workspace(client, job_id, "track")
    choice = page[page.index('<div class="field" data-when-status="applied">'):]
    assert 'name="follow_up"' in choice and '<option value="7" selected>7 days</option>' in choice
    apply(client, job_id, days_ago(1))
    assert 'name="follow_up"' not in workspace(client, job_id, "track")  # already Applied


# ---------------------------------------------------------------- when it's due

def test_due_only_while_applied_and_once_the_date_has_come(client):
    job_id = new_job(client)
    apply(client, job_id, days_ago(6))
    with client.app.state.session_factory() as session:
        application = session.get(Job, job_id).application
        assert not tracking.follow_up_due(application, TODAY)
        assert tracking.follow_up_due(application, TODAY + timedelta(days=1))
        application.tracking_status = "assessment"  # heard back
        assert not tracking.follow_up_due(application, TODAY + timedelta(days=1))


def test_a_due_reminder_is_the_next_action(client):
    job_id = new_job(client)
    apply(client, job_id, days_ago(9))
    with client.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        guide = next_action(job, job.application, get_candidate(session), TODAY)
    assert (guide.next.step, guide.next.label, guide.next.status) == (Step.TRACK, "Follow up", Status.NEEDS_YOU)
    assert guide.next.href == f"/jobs/{job_id}?step=track#follow-up"
    track = guide.state(Step.TRACK).meaning
    assert (track.status, track.note) == (Status.NEEDS_YOU, "Follow up: no reply for 9 days")
    page = client.get(f"/jobs/{job_id}").text  # opens on the Track step
    assert "No reply for 9 days. Time to follow up?" in page and "ResuApply never contacts employers" in page


def test_the_board_shows_due_follow_ups_in_a_banner_and_on_the_card(client):
    due = new_job(client, title="Data Analyst Intern")
    apply(client, due, days_ago(9))
    waiting = new_job(client, title="Platform Intern")
    apply(client, waiting, days_ago(2))
    board = built(client)
    assert [f.job.title for f in board.follow_ups] == ["Data Analyst Intern"]
    cards = {c.job.title: c.meaning for c in board.columns[1].cards}
    assert (cards["Data Analyst Intern"].status, cards["Data Analyst Intern"].note) == (
        Status.NEEDS_YOU, "Follow up: no reply, applied 9 days ago")
    assert cards["Platform Intern"].status is Status.INFO
    page = client.get("/jobs").text
    banner = page[page.index('id="follow-ups"'):page.index("</section>", page.index('id="follow-ups"'))]
    assert "1 application is due a follow-up" in banner and "no reply, applied 9 days ago" in banner
    assert f'action="/jobs/{due}/follow-up/done"' in banner and "Platform Intern" not in banner
    # The banner counts every due application, whatever the search.
    assert 'id="follow-ups"' in client.get("/jobs", params={"q": "platform"}).text


def test_no_banner_without_a_due_reminder(client):
    apply(client, new_job(client), days_ago(1))
    assert 'id="follow-ups"' not in client.get("/jobs").text


# ---------------------------------------------------------------- the actions

def test_i_followed_up_notes_it_and_clears_the_reminder(client):
    job_id = new_job(client)
    apply(client, job_id, days_ago(9))
    response = client.post(f"/jobs/{job_id}/follow-up/done", follow_redirects=False)
    assert response.headers["location"] == f"/jobs/{job_id}?step=track&msg=followed_up#follow-up"
    assert stored(client, job_id) == (None, [tracking.FOLLOWED_UP_NOTE])
    assert built(client).follow_ups == []


def test_remind_me_later_moves_the_reminder_and_returns_to_the_board(client):
    job_id = new_job(client)
    apply(client, job_id, days_ago(9))
    response = client.post(f"/jobs/{job_id}/follow-up/remind", data={"days": "3", "back": "board"}, follow_redirects=False)
    assert response.headers["location"] == "/jobs?msg=reminder_set#follow-ups"
    assert stored(client, job_id)[0] == TODAY + timedelta(days=3)
    assert "Follow-up reminder set." in client.get(response.headers["location"]).text


def test_a_reminder_can_be_cancelled_or_set_later(client):
    job_id = new_job(client)
    apply(client, job_id, days_ago(1), "")
    page = workspace(client, job_id, "track")
    assert "No reminder is set for this application." in page
    client.post(f"/jobs/{job_id}/follow-up/remind", data={"days": "10"})
    assert stored(client, job_id)[0] == TODAY + timedelta(days=10)
    cleared = client.post(f"/jobs/{job_id}/follow-up/remind", data={"days": ""}, follow_redirects=False)
    assert cleared.headers["location"].endswith("msg=reminder_cleared#follow-up")
    assert stored(client, job_id)[0] is None


@pytest.mark.parametrize("action, data", [("done", {}), ("remind", {"days": "3"})])
def test_reminders_are_only_for_applications_waiting_for_a_reply(client, action, data):
    job_id = new_job(client)  # still Saved
    response = client.post(f"/jobs/{job_id}/follow-up/{action}", data=data)
    assert response.status_code == 409 and "still waiting for a reply" in response.text
    assert 'step=track" class="step-link here"' in response.text
    assert stored(client, job_id) == (None, [])


def test_a_bad_reminder_choice_is_refused(client):
    job_id = new_job(client)
    apply(client, job_id, days_ago(1))
    before = stored(client, job_id)
    response = client.post(f"/jobs/{job_id}/follow-up/remind", data={"days": "365"})
    assert response.status_code == 422 and "Choose when to be reminded" in response.text
    assert stored(client, job_id) == before


def test_unknown_jobs_are_not_found(client):
    assert client.post("/jobs/999/follow-up/done").status_code == 404
    assert client.post("/jobs/999/follow-up/remind", data={"days": "3"}).status_code == 404


# ---------------------------------------------------------------- an existing database

def test_existing_database_gains_the_follow_up_column(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    init_db(engine)
    with make_session_factory(engine)() as session:
        jobs.create(session, jobs.clean_input(**DEMO_JOB))
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE applications DROP COLUMN follow_up_after"))
    init_db(engine)
    assert "follow_up_after" in {c["name"] for c in inspect(engine).get_columns("applications")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT follow_up_after FROM applications")).scalar_one() is None
    engine.dispose()
