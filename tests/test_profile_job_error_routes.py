"""HTTP regressions for failed saves and malformed job URL inputs."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.services import profile
from tests.conftest import SAMPLE_JOB
from tests.test_routes import client_profile, hidden, profile_form, review_and_save


def test_overlapping_profile_save_requests_return_one_conflict(client, sample_profile, monkeypatch):
    _, saved = review_and_save(client, profile_form(sample_profile))
    assert saved.status_code == 303
    data = client_profile(client)
    proposals = [data | {"summary": value} for value in ("First edit", "Second edit")]
    barrier = Barrier(2)
    normalize = profile.normalize

    def normalized_together(*args, **kwargs):
        result = normalize(*args, **kwargs)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(profile, "normalize", normalized_together)

    def submit(proposed):
        return client.post("/profile/save", data={"payload": json.dumps(proposed), "base_revision": "1"},
                           follow_redirects=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, proposals))
    assert sorted(response.status_code for response in responses) == [303, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert "changed since you reviewed it" in conflict.text
    winner_index = next(index for index, response in enumerate(responses) if response.status_code == 303)
    saved_page = client.get("/profile").text
    assert proposals[winner_index]["summary"] in saved_page
    assert "revision 2" in saved_page.lower()


def test_malformed_url_create_preserves_the_submitted_form(client):
    response = client.post("/jobs", data=SAMPLE_JOB | {"url": "https://[example.com"})
    assert response.status_code == 422
    assert "Enter a full link" in response.text
    assert 'value="https://[example.com"' in response.text
    assert SAMPLE_JOB["title"] in response.text
    assert "No jobs yet" in client.get("/jobs").text


def test_malformed_url_edit_preserves_proposal_and_saved_job(client):
    created = client.post("/jobs", data=SAMPLE_JOB, follow_redirects=False)
    path = created.headers["location"].split("?", 1)[0]
    revision = hidden(client.get(path + "/edit").text, "base_revision")
    response = client.post(path + "/edit", data=SAMPLE_JOB | {"title": "Unsaved title", "url": "https://[example.com", "base_revision": revision})
    assert response.status_code == 422
    assert 'value="Unsaved title"' in response.text
    assert 'value="https://[example.com"' in response.text
    workspace = client.get(path).text
    assert SAMPLE_JOB["title"] in workspace
    assert "Unsaved title" not in workspace


def test_authorization_country_errors_point_to_editable_form(client, sample_profile):
    sample_profile["authorization"] = [{"country": "Atlantis", "authorized": True}]
    response = client.post("/profile/review", data=profile_form(sample_profile))
    assert response.status_code == 422
    assert "recognized country name" in response.text
    assert 'value="Atlantis"' in response.text
