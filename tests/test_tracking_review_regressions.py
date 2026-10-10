"""Job edits, tracking appends and snapshot review survive overlapping sessions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html import unescape
from threading import Barrier

import pytest

from app.db import make_session_factory
from app.models import Application
from app.schemas.tracking import ReviewState, TrackingStatus
from app.services import jobs, package, profile, resume, tracking
from tests.synthetic import SAMPLE_JOB
from tests.test_package import accept_resume, approve
from tests.test_questions_routes import approve_package, complete_package, page_token, ready_job, workspace
from tests.test_review_tokens import fingerprint, record_with
from tests.test_routes import create_job, hidden, job_review


def job_values(job, **changes):
    return jobs.clean_input(**({name: getattr(job, name) for name in SAMPLE_JOB} | changes))


def test_stale_loaded_job_cannot_overwrite_a_newer_url(session):
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    with make_session_factory(session.get_bind())() as stale:
        old = jobs.get(stale, job.id)
        old_data = job_values(old, title="My title")
        jobs.update(session, job, job_values(job, url="https://example.com/new-posting"))
        with pytest.raises(jobs.JobChanged):
            jobs.update(stale, old, old_data)
    session.refresh(job)
    assert job.url == "https://example.com/new-posting" and job.title == SAMPLE_JOB["title"]


def test_job_url_conflicts_require_review_of_each_new_version(client):
    job_id = create_job(client)
    form = client.get(f"/jobs/{job_id}/edit").text
    original = {"base_revision": hidden(form, "base_revision"), "base_token": hidden(form, "base_token")}
    first = SAMPLE_JOB | original | {"url": "https://example.com/first"}
    assert client.post(f"/jobs/{job_id}/edit", data=first, follow_redirects=False).status_code == 303
    mine = SAMPLE_JOB | original | {"title": "My title"}
    conflict = client.post(f"/jobs/{job_id}/edit", data=mine, follow_redirects=False)
    assert conflict.status_code == 409 and "https://example.com/first" in conflict.text
    assert hidden(conflict.text, "base_token") == original["base_token"]
    assert client.post(f"/jobs/{job_id}/edit", data=mine, follow_redirects=False).status_code == 409
    replacement = {name: hidden(conflict.text, name) for name in ("replace_revision", "replace_token")}

    current = client.get(f"/jobs/{job_id}/edit").text
    second = SAMPLE_JOB | {name: hidden(current, name) for name in ("base_revision", "base_token")}
    second["url"] = "https://example.com/second"
    assert client.post(f"/jobs/{job_id}/edit", data=second, follow_redirects=False).status_code == 303
    again = client.post(f"/jobs/{job_id}/edit", data=mine | replacement, follow_redirects=False)
    assert again.status_code == 409 and "https://example.com/second" in again.text
    replacement = {name: hidden(again.text, name) for name in ("replace_revision", "replace_token")}
    assert client.post(f"/jobs/{job_id}/edit", data=mine | replacement, follow_redirects=False).status_code == 303
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        assert job.title == "My title" and job.url == SAMPLE_JOB["url"]


@pytest.mark.parametrize("token", [None, "", "invalid"])
def test_job_edit_without_a_valid_token_requires_review(client, token):
    job_id = create_job(client)
    mine = SAMPLE_JOB | {"title": "My title", "base_revision": "1"}
    if token is not None:
        mine["base_token"] = token
    refused = client.post(f"/jobs/{job_id}/edit", data=mine, follow_redirects=False)
    assert refused.status_code == 409 and 'value="My title"' in refused.text
    with client.app.state.session_factory() as session:
        assert jobs.get(session, job_id).title == SAMPLE_JOB["title"]
    reviewed = {name: hidden(refused.text, name) for name in ("replace_revision", "replace_token")}
    assert client.post(f"/jobs/{job_id}/edit", data=mine | reviewed, follow_redirects=False).status_code == 303


def test_job_validation_error_keeps_the_reviewed_token(client):
    job_id = create_job(client)
    reviewed = job_review(client, job_id)
    mine = SAMPLE_JOB | reviewed | {"url": "invalid", "title": "My title"}
    invalid = client.post(f"/jobs/{job_id}/edit", data=mine, follow_redirects=False)
    assert invalid.status_code == 422
    retry = {name: hidden(invalid.text, name) for name in ("base_revision", "base_token")}
    assert retry == reviewed
    winner = SAMPLE_JOB | reviewed | {"url": "https://example.com/winner"}
    assert client.post(f"/jobs/{job_id}/edit", data=winner, follow_redirects=False).status_code == 303
    corrected = SAMPLE_JOB | retry | {"title": "My title"}
    assert client.post(f"/jobs/{job_id}/edit", data=corrected, follow_redirects=False).status_code == 409


def test_job_validation_error_keeps_explicit_replacement_review(client):
    job_id = create_job(client)
    original = job_review(client, job_id)
    winner = SAMPLE_JOB | original | {"url": "https://example.com/winner"}
    client.post(f"/jobs/{job_id}/edit", data=winner, follow_redirects=False)
    conflict = client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | original, follow_redirects=False)
    reviewed = {name: hidden(conflict.text, name) for name in ("replace_revision", "replace_token")}
    invalid = client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | original | reviewed | {"title": ""},
                          follow_redirects=False)
    assert invalid.status_code == 422
    retry = {name: hidden(invalid.text, name) for name in ("base_revision", "base_token")}
    assert retry["base_token"] == reviewed["replace_token"]
    assert client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | retry | {"title": "My title"},
                       follow_redirects=False).status_code == 303


@pytest.mark.parametrize("no_op", [False, True])
def test_stale_loaded_job_edit_is_rejected_even_when_it_looks_unchanged(session, no_op):
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    with make_session_factory(session.get_bind())() as stale:
        old = jobs.get(stale, job.id)
        old_data = job_values(old, **({} if no_op else {"description": "Losing edit."}))
        jobs.update(session, job, job_values(job, description="Winning edit."))
        with pytest.raises(jobs.JobChanged, match="changed since"):
            jobs.update(stale, old, old_data)
    session.refresh(job)
    assert job.description == "Winning edit." and job.revision == 2


def test_losing_job_edit_cannot_keep_approval_for_a_different_description(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    with make_session_factory(session.get_bind())() as stale:
        old = jobs.get(stale, job.id)
        old_data = job_values(old, description="Description nobody reviewed.")
        jobs.update(session, job, job_values(job, description="Description actually reviewed."))
        accept_resume(session, job, candidate)
        approve(session, job, candidate)
        approved = job.application.approval.model_dump_json()
        with pytest.raises(jobs.JobChanged):
            jobs.update(stale, old, old_data)
    session.refresh(job)
    session.refresh(job.application)
    assert job.description == "Description actually reviewed."
    assert job.application.approval.model_dump_json() == approved
    assert package.review_state(job, job.application, candidate) is ReviewState.APPROVED


def test_concurrent_job_edits_issue_one_revision_and_one_conflict(session):
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    factory, barrier = make_session_factory(session.get_bind()), Barrier(2)

    def edit(description):
        with factory() as writer:
            loaded = jobs.get(writer, job.id)
            data = job_values(loaded, description=description)
            barrier.wait(timeout=5)
            try:
                jobs.update(writer, loaded, data)
                return "saved", description
            except jobs.JobChanged:
                return "conflict", description

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["First edit.", "Second edit."]))
    assert sorted(result[0] for result in results) == ["conflict", "saved"]
    session.refresh(job)
    assert job.revision == 2
    assert job.description == next(description for outcome, description in results if outcome == "saved")


@pytest.mark.parametrize("base_revision", [None, "", "bogus", "0", "-1", "99"])
def test_job_edit_requires_the_reviewed_revision_without_saving(client, base_revision):
    job_id = create_job(client)
    data = SAMPLE_JOB | {"title": "Unsaved title", "base_token": job_review(client, job_id)["base_token"]}
    if base_revision is not None:
        data["base_revision"] = base_revision
    response = client.post(f"/jobs/{job_id}/edit", data=data, follow_redirects=False)
    assert response.status_code == 409
    assert 'value="Unsaved title"' in response.text
    assert "start again from the saved version" in response.text
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        assert job.title == SAMPLE_JOB["title"] and job.revision == 1


def test_job_edit_from_an_old_page_preserves_the_proposal_and_current_data(client):
    job_id = create_job(client)
    reviewed = job_review(client, job_id)
    first = client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | reviewed | {"title": "Saved title"},
                        follow_redirects=False)
    assert first.status_code == 303
    response = client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | reviewed | {"title": "Unsaved title"},
                           follow_redirects=False)
    assert response.status_code == 409 and "changed since" in unescape(response.text)
    assert 'value="Unsaved title"' in response.text
    assert hidden(response.text, "base_revision") == reviewed["base_revision"]
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        assert job.title == "Saved title" and job.revision == 2


def test_job_edit_conflict_shows_the_saved_job_and_can_be_saved_over_it(client):
    job_id = create_job(client)
    reviewed = job_review(client, job_id)
    client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | reviewed | {"title": "Saved title"},
                follow_redirects=False)
    mine = SAMPLE_JOB | reviewed | {"title": "My title"}
    conflict = client.post(f"/jobs/{job_id}/edit", data=mine, follow_redirects=False)
    assert conflict.status_code == 409
    assert "The job as it is saved now (revision 2)" in conflict.text and "Title: Saved title" in conflict.text

    # Resubmitting unchanged is refused again: only the explicit box saves over the newer version.
    assert client.post(f"/jobs/{job_id}/edit", data=mine, follow_redirects=False).status_code == 409
    replace = hidden(conflict.text, "replace_revision")
    assert replace == "2"
    saved = client.post(f"/jobs/{job_id}/edit", data=mine | {"replace_revision": replace,
                        "replace_token": hidden(conflict.text, "replace_token")}, follow_redirects=False)
    assert saved.status_code == 303
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        assert job.title == "My title" and job.revision == 3


def test_job_edit_replace_box_is_refused_when_the_job_changed_again(client):
    job_id = create_job(client)
    first = job_review(client, job_id)
    client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | first | {"title": "Second"}, follow_redirects=False)
    second = job_review(client, job_id)
    client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | second | {"title": "Third"}, follow_redirects=False)
    stale = SAMPLE_JOB | first | {"title": "Mine", "replace_revision": "2", "replace_token": second["base_token"]}
    response = client.post(f"/jobs/{job_id}/edit", data=stale, follow_redirects=False)
    assert response.status_code == 409 and hidden(response.text, "replace_revision") == "3"
    with client.app.state.session_factory() as session:
        assert jobs.get(session, job_id).title == "Third"


def test_url_edit_changes_review_token_without_invalidating_resume(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    accept_resume(session, job, candidate)
    approve(session, job, candidate)
    token = package.package_token(job, job.application, candidate)
    jobs.update(session, job, job_values(job, url="https://example.com/replacement"))
    assert job.revision == 1 and not resume.state(job, candidate).accepted_stale
    assert package.review_state(job, job.application, candidate) is ReviewState.APPROVED
    assert package.package_token(job, job.application, candidate) != token
    with pytest.raises(package.PackageChanged, match="changed since you reviewed"):
        package.record_applied(session, job, candidate, token)
    assert job.application.submitted_snapshots == [] and job.application.status is TrackingStatus.SAVED


def test_old_snapshot_form_cannot_record_an_unreviewed_job_url(client, sample_profile):
    job_id = ready_job(client, sample_profile)
    complete_package(client, job_id)
    assert approve_package(client, job_id).status_code == 303
    old_token = page_token(workspace(client, job_id), "package_token")
    reviewed = job_review(client, job_id)
    changed = client.post(f"/jobs/{job_id}/edit", data=SAMPLE_JOB | reviewed | {"url": "https://example.com/replacement"},
                          follow_redirects=False)
    assert changed.status_code == 303
    before = fingerprint(client, job_id)
    rejected = record_with(client, job_id, package_token=old_token)
    assert rejected.status_code == 409 and "changed since you reviewed" in unescape(rejected.text)
    assert fingerprint(client, job_id) == before
    current = page_token(rejected.text, "package_token")
    assert current != old_token
    assert record_with(client, job_id, package_token=current).status_code == 303
    with client.app.state.session_factory() as session:
        job = jobs.get(session, job_id)
        assert job.revision == 1
        assert job.application.submitted_snapshots[0].job["url"] == "https://example.com/replacement"


def test_stale_notes_and_status_saves_preserve_both_updates(session):
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    with make_session_factory(session.get_bind())() as stale:
        old_application = stale.get(Application, job.application.id)
        assert old_application.notes == [] and len(old_application.status_history) == 1
        tracking.add_note(session, job.application, "First note")
        tracking.change_status(session, job.application, "assessment", note="First status")
        tracking.add_note(stale, old_application, "Second note")
        tracking.change_status(stale, old_application, "interview", note="Second status")
    session.refresh(job.application)
    assert [note.text for note in job.application.notes] == ["First note", "Second note"]
    assert [event.status.value for event in job.application.status_history] == ["saved", "assessment", "interview"]
    assert [event.note for event in job.application.status_history] == ["", "First status", "Second status"]


@pytest.mark.parametrize("operation", ["notes", "status"])
def test_concurrent_tracking_appends_preserve_every_entry(session, operation):
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    application_id = job.application.id
    factory, barrier = make_session_factory(session.get_bind()), Barrier(2)
    values = ["First note", "Second note"] if operation == "notes" else ["assessment", "interview"]

    def append(value):
        with factory() as writer:
            application = writer.get(Application, application_id)
            assert application.notes == [] and len(application.status_history) == 1
            barrier.wait(timeout=5)
            if operation == "notes":
                tracking.add_note(writer, application, value)
            else:
                tracking.change_status(writer, application, value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(append, values))
    session.refresh(job.application)
    if operation == "notes":
        assert sorted(note.text for note in job.application.notes) == sorted(values)
    else:
        history = job.application.status_history
        assert history[0].status is TrackingStatus.SAVED and len(history) == 3
        assert sorted(event.status.value for event in history[1:]) == sorted(values)
        assert job.application.status == history[-1].status


def test_stale_tracking_status_preserves_applied_event_and_snapshot(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    accept_resume(session, job, candidate)
    approve(session, job, candidate)
    with make_session_factory(session.get_bind())() as stale:
        old_application = stale.get(Application, job.application.id)
        assert len(old_application.status_history) == 1 and old_application.submitted_snapshots == []
        token = package.package_token(job, job.application, candidate)
        snapshot = package.record_applied(session, job, candidate, token)
        frozen = snapshot.model_dump_json()
        tracking.change_status(stale, old_application, "interview", note="Recruiter replied")
    session.refresh(job.application)
    assert [event.status.value for event in job.application.status_history] == ["saved", "applied", "interview"]
    assert job.application.applied_on == snapshot.submitted_on
    assert [stored.model_dump_json() for stored in job.application.submitted_snapshots] == [frozen]


def test_stale_duplicate_status_is_rejected_against_current_state(session):
    job = jobs.create(session, jobs.clean_input(**SAMPLE_JOB))
    with make_session_factory(session.get_bind())() as stale:
        old_application = stale.get(Application, job.application.id)
        assert old_application.status is TrackingStatus.SAVED
        tracking.change_status(session, job.application, "interview")
        with pytest.raises(tracking.TrackingError, match="already Interview"):
            tracking.change_status(stale, old_application, "interview")
    session.refresh(job.application)
    assert [event.status.value for event in job.application.status_history] == ["saved", "interview"]
