"""Confirming, accepting, approving and recording act only on what the user reviewed (synthetic data only).

Each form carries a token of what its page showed. A missing or different token is refused with
409 and the current content, and the refused request changes nothing.
"""

from __future__ import annotations

import re
from html import unescape

import pytest

from app.models import Job
from tests.test_questions_routes import (
    AUTH_Q, BULLET, WHY_Q, add, approve_package, complete_package, draft_reply, page_token, post, qid_of,
    ready_job, record, review_part, row, workspace,
)
from tests.conftest import SAMPLE_JOB
from tests.test_resume_routes import accept, approval, manual_proposal
from tests.test_routes import client_profile, profile_form, review_and_save

SPONSOR_Q = "Will you now or in the future require visa sponsorship?"
PROJECT_Q = "Describe a project you are proud of"
PROJECT = "Created a Python chat bot that tracks team tasks in SQLite"
REVIEW_AGAIN = "changed since you reviewed it"


def fingerprint(client, job_id) -> tuple:
    """Everything a refused request must leave alone."""
    with client.app.state.session_factory() as session:
        app_ = session.get(Job, job_id).application
        return (
            [a.model_dump_json() for a in app_.answers],
            [d.model_dump_json() for d in app_.answer_drafts],
            app_.approval.model_dump_json() if app_.approval else None,
            [s.model_dump_json() for s in app_.submitted_snapshots],
            app_.tracking_status,
            len(app_.status_history),
        )


def change_phone(client, phone="+1 555 0177"):
    """A profile edit that leaves every answer's value as it was, but moves the revision on."""
    edited = client_profile(client)
    edited["contact"]["phone"] = phone
    review_and_save(client, profile_form(edited))


def another_job(client) -> int:
    """A second job with an accepted resume, for the same (already saved) profile."""
    created = client.post("/jobs", data=SAMPLE_JOB, follow_redirects=False)
    job_id = int(re.match(r"/jobs/(\d+)", created.headers["location"]).group(1))
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    return job_id


def refused(response, client, job_id, before):
    assert response.status_code == 409, response.status_code
    assert REVIEW_AGAIN in unescape(response.text)
    assert fingerprint(client, job_id) == before
    return response.text


# ---------------------------------------------------------------- confirmation_token

@pytest.fixture
def confirmable(client, sample_profile):
    """Two sensitive-factual questions, each showing a profile value to confirm."""
    job_id = ready_job(client, sample_profile)
    auth = qid_of(add(client, job_id, AUTH_Q))
    sponsor = qid_of(add(client, job_id, SPONSOR_Q))
    return job_id, auth, sponsor


def confirm_with(client, job_id, qid, **data):
    return client.post(f"/jobs/{job_id}/questions/{qid}/confirm", data=data, follow_redirects=False)


def test_a_reviewed_confirmation_token_confirms(client, confirmable):
    job_id, auth, _ = confirmable
    token = page_token(row(workspace(client, job_id), auth), "confirmation_token")
    assert confirm_with(client, job_id, auth, confirmation_token=token).status_code == 303
    assert "From profile (confirmed)" in row(workspace(client, job_id), auth)


@pytest.mark.parametrize("token", [None, "", "0123456789abcdef"])
def test_a_missing_or_wrong_confirmation_token_is_refused(client, confirmable, token):
    job_id, auth, _ = confirmable
    before = fingerprint(client, job_id)
    data = {} if token is None else {"confirmation_token": token}
    page = refused(confirm_with(client, job_id, auth, **data), client, job_id, before)
    shown = row(page, auth)
    assert "<strong>Yes</strong>" in shown and 'role="alert"' in shown and "confirm it again" in shown
    assert page_token(shown, "confirmation_token")  # the current value can be reviewed and confirmed


def test_a_confirmation_token_from_another_question_or_job_is_refused(client, confirmable):
    job_id, auth, sponsor = confirmable
    page = workspace(client, job_id)
    before = fingerprint(client, job_id)
    other_question = page_token(row(page, sponsor), "confirmation_token")
    refused(confirm_with(client, job_id, auth, confirmation_token=other_question), client, job_id, before)

    other_job = another_job(client)
    same = qid_of(add(client, other_job, AUTH_Q, required=False))
    assert same == auth
    other_job_token = page_token(row(workspace(client, other_job), same), "confirmation_token")
    refused(confirm_with(client, job_id, auth, confirmation_token=other_job_token), client, job_id, before)


def test_a_confirmation_token_from_an_outdated_page_is_refused(client, confirmable):
    job_id, auth, _ = confirmable
    old = page_token(row(workspace(client, job_id), auth), "confirmation_token")
    change_phone(client)  # the value is still "Yes", but the profile it came from changed
    before = fingerprint(client, job_id)
    page = refused(confirm_with(client, job_id, auth, confirmation_token=old), client, job_id, before)
    current = page_token(row(page, auth), "confirmation_token")
    assert current != old
    assert confirm_with(client, job_id, auth, confirmation_token=current).status_code == 303


# ---------------------------------------------------------------- draft_token

@pytest.fixture
def drafted(ai_app_client, fake_ai, sample_profile):
    client = ai_app_client
    job_id = ready_job(client, sample_profile)
    approval(client)
    why = qid_of(add(client, job_id, WHY_Q))
    project = qid_of(add(client, job_id, PROJECT_Q))
    fake_ai.push(draft_reply({"question_id": why, "text": BULLET, "sources": ["exp-1-b1"]},
                             {"question_id": project, "text": PROJECT, "sources": ["proj-1-b1"]}))
    assert client.post(f"/jobs/{job_id}/questions/draft", follow_redirects=False).status_code == 303
    return client, job_id, why, project


def accept_with(client, job_id, qid, **data):
    return client.post(f"/jobs/{job_id}/questions/{qid}/draft/accept", data=data, follow_redirects=False)


def test_a_reviewed_draft_token_accepts_the_draft(drafted):
    client, job_id, why, _ = drafted
    token = page_token(row(workspace(client, job_id), why), "draft_token")
    assert accept_with(client, job_id, why, draft_token=token).status_code == 303
    assert fingerprint(client, job_id)[0][0].count(BULLET) == 1


@pytest.mark.parametrize("token", [None, "", "0123456789abcdef"])
def test_a_missing_or_wrong_draft_token_is_refused(drafted, token):
    client, job_id, why, _ = drafted
    before = fingerprint(client, job_id)
    data = {} if token is None else {"draft_token": token}
    page = refused(accept_with(client, job_id, why, **data), client, job_id, before)
    shown = row(page, why)
    assert BULLET in shown and "accept it again" in shown and page_token(shown, "draft_token")


def test_a_draft_token_from_another_question_or_job_is_refused(drafted, fake_ai):
    client, job_id, why, project = drafted
    before = fingerprint(client, job_id)
    other_question = page_token(row(workspace(client, job_id), project), "draft_token")
    refused(accept_with(client, job_id, why, draft_token=other_question), client, job_id, before)

    other_job = another_job(client)
    same = qid_of(add(client, other_job, WHY_Q))
    fake_ai.push(draft_reply({"question_id": same, "text": BULLET, "sources": ["exp-1-b1"]}))
    assert client.post(f"/jobs/{other_job}/questions/draft", follow_redirects=False).status_code == 303
    other_job_token = page_token(row(workspace(client, other_job), same), "draft_token")
    refused(accept_with(client, job_id, why, draft_token=other_job_token), client, job_id, before)


def test_a_draft_token_from_an_outdated_page_is_refused(drafted, fake_ai):
    client, job_id, why, project = drafted
    old = page_token(row(workspace(client, job_id), why), "draft_token")
    newer = "Wrote unit tests for the billing module"
    fake_ai.push(draft_reply({"question_id": why, "text": newer, "sources": ["exp-1-b3"]},
                             {"question_id": project, "text": PROJECT, "sources": ["proj-1-b1"]}))
    assert client.post(f"/jobs/{job_id}/questions/draft", data={"force": "1"},
                       follow_redirects=False).status_code == 303
    before = fingerprint(client, job_id)
    page = refused(accept_with(client, job_id, why, draft_token=old), client, job_id, before)
    assert newer in row(page, why)  # the draft that is there now, to review before accepting


def test_an_older_draft_is_accepted_when_its_claims_still_hold(drafted):
    client, job_id, why, _ = drafted
    token = page_token(row(workspace(client, job_id), why), "draft_token")
    change_phone(client)
    assert "Drafted before your latest profile edit" in row(workspace(client, job_id), why)
    assert accept_with(client, job_id, why, draft_token=token).status_code == 303


# ---------------------------------------------------------------- package_token: approval

@pytest.fixture
def approvable(client, sample_profile):
    job_id = ready_job(client, sample_profile)
    _, why, _ = complete_package(client, job_id)
    return job_id, why


def approve_with(client, job_id, **data):
    return client.post(f"/jobs/{job_id}/approve", data=data, follow_redirects=False)


def test_a_reviewed_package_token_approves(client, approvable):
    job_id, _ = approvable
    token = page_token(review_part(workspace(client, job_id)), "package_token")
    assert approve_with(client, job_id, package_token=token).status_code == 303
    assert fingerprint(client, job_id)[2] is not None


@pytest.mark.parametrize("token", [None, "", "0123456789abcdef"])
def test_approval_with_a_missing_or_wrong_package_token_is_refused(client, approvable, token):
    job_id, _ = approvable
    before = fingerprint(client, job_id)
    data = {} if token is None else {"package_token": token}
    page = refused(approve_with(client, job_id, **data), client, job_id, before)
    review = review_part(page)
    assert "Review the package as it is now" in review and page_token(review, "package_token")


def test_approval_with_another_jobs_package_token_is_refused(client, approvable):
    job_id, _ = approvable
    other_job = another_job(client)
    complete_package(client, other_job)
    before = fingerprint(client, job_id)
    other_token = page_token(review_part(workspace(client, other_job)), "package_token")
    refused(approve_with(client, job_id, package_token=other_token), client, job_id, before)


def test_approval_from_an_outdated_page_is_refused(client, approvable):
    job_id, why = approvable
    old = page_token(review_part(workspace(client, job_id)), "package_token")
    assert post(client, job_id, why, "answer", text="An answer written in another tab.").status_code == 303
    before = fingerprint(client, job_id)
    page = refused(approve_with(client, job_id, package_token=old), client, job_id, before)
    assert "An answer written in another tab." in row(page, why)
    current = page_token(review_part(page), "package_token")
    assert approve_with(client, job_id, package_token=current).status_code == 303


# ---------------------------------------------------------------- package_token: recording what was submitted

@pytest.fixture
def approved(client, approvable):
    job_id, why = approvable
    assert approve_package(client, job_id).status_code == 303
    return job_id, why


def record_with(client, job_id, **data):
    return client.post(f"/jobs/{job_id}/status", data={"status": "applied", "on": "", "note": "", "snapshot": "1",
                                                       **data}, follow_redirects=False)


@pytest.mark.parametrize("token", [None, "", "0123456789abcdef"])
def test_recording_with_a_missing_or_wrong_package_token_is_refused(client, approved, token):
    job_id, _ = approved
    before = fingerprint(client, job_id)
    data = {} if token is None else {"package_token": token}
    page = refused(record_with(client, job_id, **data), client, job_id, before)
    assert "Save the approved package as what I submitted" in page and page_token(page, "package_token")


def test_recording_with_another_jobs_package_token_is_refused(client, approved):
    job_id, _ = approved
    other_job = another_job(client)
    complete_package(client, other_job)
    assert approve_package(client, other_job).status_code == 303
    before = fingerprint(client, job_id)
    other_token = page_token(workspace(client, other_job), "package_token")
    refused(record_with(client, job_id, package_token=other_token), client, job_id, before)


def test_recording_from_an_outdated_page_is_refused(client, approved):
    job_id, why = approved
    old = page_token(workspace(client, job_id), "package_token")
    # Another tab changes an answer and approves again: still Approved, but not the package on the old page.
    assert post(client, job_id, why, "answer", text="A later answer.").status_code == 303
    assert approve_package(client, job_id).status_code == 303
    before = fingerprint(client, job_id)
    refused(record_with(client, job_id, package_token=old), client, job_id, before)
    assert record(client, job_id).status_code == 303
    snapshots = fingerprint(client, job_id)[3]
    assert len(snapshots) == 1 and "A later answer." in snapshots[0]


def test_tracking_notes_do_not_change_the_package_token(client, approved):
    job_id, _ = approved
    token = page_token(workspace(client, job_id), "package_token")
    assert client.post(f"/jobs/{job_id}/notes", data={"text": "Recruiter called."}).status_code == 200
    assert page_token(workspace(client, job_id), "package_token") == token
    assert record_with(client, job_id, package_token=token).status_code == 303
