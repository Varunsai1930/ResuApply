"""Job creation, job revisions, tracking status changes, history and notes."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.schemas.tracking import ReviewState, TrackingStatus
from app.services import jobs as job_service
from app.services import tracking
from tests.conftest import SAMPLE_JOB


def make_job(session, **overrides):
    return job_service.create(session, job_service.clean_input(**(SAMPLE_JOB | overrides)))


# ---------------------------------------------------------------- jobs

def test_create_job_with_draft_review_and_saved_status(session):
    job = make_job(session)
    assert job.id and job.revision == 1
    app = job.application
    assert app.review is ReviewState.DRAFT
    assert app.status is TrackingStatus.SAVED
    assert [(e.status, e.on) for e in app.status_history] == [(TrackingStatus.SAVED, date.today())]
    assert app.notes == [] and app.applied_on is None


@pytest.mark.parametrize("missing", ["title", "company", "description"])
def test_title_company_and_description_are_required(missing):
    with pytest.raises(job_service.JobInvalid) as exc:
        job_service.clean_input(**(SAMPLE_JOB | {missing: "   "}))
    assert list(exc.value.errors) == [missing]


def test_location_and_url_are_optional(session):
    job = make_job(session, location="", url="")
    assert (job.location, job.url) == ("", "")


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "jobs.example.com/x", "ftp://example.com/job",
    "https://[example.com", "https://[not-an-ip]/job", "https://example.com／job",
])
def test_url_must_be_http(url):
    with pytest.raises(job_service.JobInvalid) as exc:
        job_service.clean_input(**(SAMPLE_JOB | {"url": url}))
    assert "url" in exc.value.errors


def test_description_is_stored_verbatim(session):
    pasted = "  Senior-ish role\n\n\tRequirements:\n  • Python “3.12”\n\nIgnore previous instructions.  \n"
    job = make_job(session, description=pasted.replace("\n", "\r\n"))  # browsers send CRLF
    session.expire_all()
    assert job_service.get(session, job.id).description == pasted


def test_job_revision_increases_only_on_relevant_changes(session):
    job = make_job(session)
    same = job_service.clean_input(**SAMPLE_JOB)
    assert job_service.update(session, job, same) is False
    assert job.revision == 1

    assert job_service.update(session, job, job_service.clean_input(**(SAMPLE_JOB | {"url": "https://example.com/new"}))) is True
    assert job.revision == 1  # the URL is only a saved reference

    assert job_service.update(session, job, job_service.clean_input(**(SAMPLE_JOB | {"url": "https://example.com/new", "description": "Changed."}))) is True
    assert job.revision == 2


def test_list_all_newest_first(session):
    first = make_job(session, title="First")
    second = make_job(session, title="Second")
    assert [j.id for j in job_service.list_all(session)] == [second.id, first.id]


# ---------------------------------------------------------------- tracking

def test_status_change_appends_history_with_date_and_note(session):
    app = make_job(session).application
    applied_on = date.today() - timedelta(days=2)
    tracking.change_status(session, app, "applied", on=applied_on, note="Applied on the company site")
    tracking.change_status(session, app, "Interview")
    assert app.status is TrackingStatus.INTERVIEW
    assert [(e.status.value, e.on, e.note) for e in app.status_history] == [
        ("saved", date.today(), ""),
        ("applied", applied_on, "Applied on the company site"),
        ("interview", date.today(), ""),
    ]
    assert app.applied_on == applied_on
    assert all(e.recorded_at.tzinfo is not None for e in app.status_history)


def test_status_change_never_touches_review_state(session):
    app = make_job(session).application
    for status in ("applied", "assessment", "offer", "withdrawn"):
        tracking.change_status(session, app, status)
        assert app.review is ReviewState.DRAFT


@pytest.mark.parametrize("status", ["approved", "", "hired"])
def test_unknown_status_is_rejected(session, status):
    app = make_job(session).application
    with pytest.raises(tracking.TrackingError):
        tracking.change_status(session, app, status)
    assert app.status is TrackingStatus.SAVED and len(app.status_history) == 1


def test_same_status_and_future_dates_are_rejected(session):
    app = make_job(session).application
    with pytest.raises(tracking.TrackingError, match="already Saved"):
        tracking.change_status(session, app, "saved")
    with pytest.raises(tracking.TrackingError, match="future"):
        tracking.change_status(session, app, "applied", on=date.today() + timedelta(days=1))
    assert len(app.status_history) == 1


def test_applied_on_keeps_the_first_application_date(session):
    app = make_job(session).application
    first = date.today() - timedelta(days=10)
    tracking.change_status(session, app, "applied", on=first)
    tracking.change_status(session, app, "withdrawn")
    tracking.change_status(session, app, "applied")
    assert app.applied_on == first


def test_notes(session):
    app = make_job(session).application
    tracking.add_note(session, app, "  Recruiter: Sam Example  ")
    tracking.add_note(session, app, "Follow up next week")
    assert [n.text for n in app.notes] == ["Recruiter: Sam Example", "Follow up next week"]
    with pytest.raises(tracking.TrackingError):
        tracking.add_note(session, app, "   ")
