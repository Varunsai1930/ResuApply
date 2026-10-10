"""Confirmed source content survives restarts and changed evidence needs a user action."""

import re

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import BASE_URL
from tests.test_assessment_routes import card, evidence_token, setup
from tests.test_routes import client_profile, profile_form, review_and_save


def test_changed_evidence_requires_explicit_reconfirmation_after_restart(settings, sample_profile):
    with TestClient(create_app(settings), base_url=BASE_URL) as first:
        job_id = setup(first, sample_profile)
        response = first.post(f"/jobs/{job_id}/requirements/r4/evidence", data={"sources": "exp-1-b1", "review_token": evidence_token(first, job_id, "r4")},
                              follow_redirects=False)
        assert response.status_code == 303
        assert "check-met" in card(first.get(f"/jobs/{job_id}?step=requirements").text, "r4")

    with TestClient(create_app(settings), base_url=BASE_URL) as restarted:
        api_card = card(restarted.get(f"/jobs/{job_id}?step=requirements").text, "r4")
        assert "check-met" in api_card and "Reconfirm" not in api_card
        proposed = client_profile(restarted)
        proposed["experience"][0]["bullets"][0]["text"] = "Built a REST API for a new reporting service"
        _, saved = review_and_save(restarted, profile_form(proposed))
        assert saved.status_code == 303
        api_card = card(restarted.get(f"/jobs/{job_id}?step=requirements").text, "r4")
        assert "check-unknown" in api_card and "check-met" not in api_card
        assert "Built a REST API for a new reporting service" in api_card
        form = re.search(
            r'<form method="post" action="([^"]+)"[^>]*>\s*'
            r'<input type="hidden" name="sources" value="([^"]+)">\s*'
            r'<input type="hidden" name="review_token" value="([^"]+)">\s*'
            r'<button[^>]*>Reconfirm</button>', api_card,
        )
        assert form, "The changed source must have a working reconfirmation form"
        response = restarted.post(form.group(1), data={"sources": form.group(2), "review_token": form.group(3)}, follow_redirects=False)
        assert response.status_code == 303
        api_card = card(restarted.get(f"/jobs/{job_id}?step=requirements").text, "r4")
        assert "check-met" in api_card and "Reconfirm" not in api_card
