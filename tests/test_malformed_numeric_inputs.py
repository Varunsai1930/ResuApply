"""Damaged numeric inputs receive validation errors without changing saved work."""

import pytest

from app.models import Candidate, Job
from app.services.tracking import FOLLOW_UP_CHOICES, TrackingError, parse_days
from tests.conftest import SAMPLE_JOB


@pytest.mark.parametrize("endpoint", ["/profile/save", "/profile/edit"])
def test_oversized_json_integer_is_a_damaged_profile_response(client, endpoint):
    response = client.post(endpoint, data={"payload": '{"number":' + "9" * 5000 + '}', "base_revision": "0"})
    assert response.status_code == 400
    assert "missing or damaged" in response.text
    with client.app.state.session_factory() as session:
        assert session.get(Candidate, 1) is None


def test_oversized_import_note_is_ignored_in_profile_review(client):
    response = client.post("/profile/review", data={"contact-name": "Example", "imported": '{"number":' + "9" * 5000 + '}'})
    assert response.status_code == 200


@pytest.mark.parametrize("reminder", ["9" * 5000, "0" * 5000, "٣", "7.0"])
def test_invalid_reminder_choice_does_not_raise_an_integer_conversion_error(reminder):
    with pytest.raises(TrackingError):
        parse_days(reminder, FOLLOW_UP_CHOICES)


def test_leading_zero_reminder_choice_remains_compatible():
    assert parse_days("0007", FOLLOW_UP_CHOICES) == 7


def test_oversized_reminder_form_values_preserve_the_saved_status(client):
    response = client.post("/jobs", data=SAMPLE_JOB, follow_redirects=False)
    path = response.headers["location"].split("?", 1)[0]
    job_id = int(path.rsplit("/", 1)[1])
    response = client.post(path + "/status", data={"status": "applied", "follow_up": "9" * 5000})
    assert response.status_code == 422
    with client.app.state.session_factory() as session:
        assert session.get(Job, job_id).application.tracking_status == "saved"
    assert client.post(path + "/status", data={"status": "applied", "follow_up": "7"}, follow_redirects=False).status_code == 303
    with client.app.state.session_factory() as session:
        reminder = session.get(Job, job_id).application.follow_up_after
    response = client.post(path + "/follow-up/remind", data={"days": "9" * 5000})
    assert response.status_code == 422
    with client.app.state.session_factory() as session:
        assert session.get(Job, job_id).application.follow_up_after == reminder
