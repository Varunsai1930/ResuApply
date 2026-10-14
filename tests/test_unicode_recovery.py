"""Reject damaged new profile text and recover legacy text through an explicit reviewed save."""

import copy
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import StatementError

from app.main import create_app
from app.models import Candidate
from app.schemas.profile import Profile
from app.schemas.tracking import ReviewState
from app.services import jobs, outbound, package, profile
from app.unicode_text import has_invalid_unicode, repair_unicode
from tests.conftest import BASE_URL, NO_NETWORK
from tests.test_resume_routes import accept, manual_proposal, setup
from tests.test_routes import hidden, profile_form, review_and_save


@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_new_damaged_unicode_is_rejected_before_persistence(client, sample_profile, surrogate):
    review_and_save(client, profile_form(sample_profile))
    with client.app.state.session_factory() as session:
        candidate = profile.get_candidate(session)
        original = candidate.profile.model_dump()
    damaged = copy.deepcopy(original)
    damaged["contact"]["name"] += surrogate
    for endpoint in ("/profile/save", "/profile/edit"):
        response = client.post(endpoint, data={"payload": json.dumps(damaged), "base_revision": "1"})
        assert response.status_code == 400
        assert "missing or damaged" in response.text
    with client.app.state.session_factory() as session:
        candidate = profile.get_candidate(session)
        assert candidate.profile.model_dump() == original
        assert candidate.revision == 1
        with pytest.raises(profile.ProfileInvalid, match="invalid"):
            profile.save(session, damaged, base_revision=1)
    with pytest.raises(ValidationError, match="unreadable Unicode"):
        Profile.model_validate(damaged)
    assert client.get("/profile").status_code == 200


@pytest.mark.parametrize("field", ["summary", "preferences", "skills"])
def test_nested_profile_unicode_is_validated(sample_profile, field):
    if field == "summary":
        sample_profile[field] = "\ud800"
    elif field == "preferences":
        sample_profile[field]["roles"] = ["\ud800"]
    else:
        sample_profile[field][0]["name"] = "\ud800"
    with pytest.raises(profile.ProfileInvalid):
        profile.normalize(sample_profile)


def test_mutating_a_profile_model_cannot_bypass_column_validation(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    modified = candidate.profile.model_copy(deep=True)
    modified.contact.name = "Damaged \ud800name"
    candidate.profile = modified
    with pytest.raises(StatementError):
        session.commit()
    session.rollback()
    assert profile.get_candidate(session).profile.contact.name == sample_profile["contact"]["name"]


def test_legacy_damage_is_reviewable_after_restart_and_changes_only_on_save(settings, sample_profile):
    with TestClient(create_app(settings, greenhouse_transport=NO_NETWORK), base_url=BASE_URL) as client:
        review_and_save(client, profile_form(sample_profile))
        with client.app.state.session_factory() as session:
            original = profile.get_candidate(session).profile.model_dump()
            damaged = copy.deepcopy(original)
            damaged["contact"]["name"] = "Jordan \ud800Example"
            damaged["projects"][0]["bullets"][0]["text"] += "\udfff"
            raw = json.dumps(damaged)
            session.execute(text("UPDATE candidates SET profile = :profile WHERE id = 1"), {"profile": raw})
            session.commit()
    # Restart against the same legacy database; ordinary loading must not overwrite it.
    with TestClient(create_app(settings, greenhouse_transport=NO_NETWORK), base_url=BASE_URL) as client:
        for endpoint in ("/", "/jobs", "/profile", "/profile/edit", "/profile/sharing"):
            assert client.get(endpoint).status_code == 200, endpoint
        assert "Some saved profile text could not be read" in client.get("/profile/edit").text
        with client.app.state.session_factory() as session:
            candidate = profile.get_candidate(session)
            assert candidate.profile.unicode_recovered
            assert candidate.revision == 1
            assert "\ufffd" in candidate.profile.contact.name
            assert session.scalar(text("SELECT profile FROM candidates WHERE id = 1")) == raw
            job = jobs.create(session, jobs.clean_input("Engineer", "Example", "Build things."))
            assert any("Repair the unreadable profile text" in item for item in package.check(job, job.application, candidate)[0])
            assert not outbound.state(session, settings.model_copy(update={"openrouter_model_trust": "trusted"}), candidate).ready
            with pytest.raises(outbound.OutboundError, match="Repair"):
                outbound.approve(session, candidate, "unused", set(), {})
            data = candidate.profile.model_dump()
            counters = dict(candidate.id_counters)
        review = client.post("/profile/review", data=profile_form(data))
        assert review.status_code == 200
        assert "Unreadable profile text" in review.text
        saved = client.post("/profile/save", data={"payload": hidden(review.text, "payload"),
                                                   "base_revision": hidden(review.text, "base_revision")},
                            follow_redirects=False)
        assert saved.status_code == 303
        with client.app.state.session_factory() as session:
            candidate = session.get(Candidate, 1)
            assert not candidate.profile.unicode_recovered
            assert candidate.revision == 2
            assert candidate.id_counters == counters
            assert not has_invalid_unicode(json.loads(session.scalar(text("SELECT profile FROM candidates WHERE id = 1"))))


def test_valid_non_ascii_profile_text_roundtrips(client, sample_profile):
    sample_profile["contact"]["name"] = "Zoë 開発者 😀"
    _, saved = review_and_save(client, profile_form(sample_profile))
    assert saved.status_code == 303
    assert sample_profile["contact"]["name"] in client.get("/profile").text
    assert repair_unicode(sample_profile) == sample_profile


def test_legacy_damage_cannot_keep_an_existing_package_approved(client, sample_profile):
    job_id = setup(client, sample_profile)
    assert accept(client, job_id, manual_proposal(client, job_id)).status_code == 303
    with client.app.state.session_factory() as session:
        candidate = profile.get_candidate(session)
        job = jobs.get(session, job_id)
        package.approve(session, job, candidate, package.package_token(job, job.application, candidate))
        damaged = candidate.profile.model_dump()
        damaged["contact"]["name"] += "\ud800"
        session.execute(text("UPDATE candidates SET profile = :profile WHERE id = 1"),
                        {"profile": json.dumps(damaged)})
        session.commit()
    with client.app.state.session_factory() as session:
        candidate = profile.get_candidate(session)
        job = jobs.get(session, job_id)
        assert package.review_state(job, job.application, candidate) is ReviewState.STALE
        with pytest.raises(package.PackageError):
            package.approve(session, job, candidate, package.package_token(job, job.application, candidate))
        repaired = candidate.profile.model_dump()
        profile.save(session, repaired, base_revision=1)
        assert package.review_state(job, job.application, candidate) is ReviewState.STALE


def test_legacy_surrogate_pairs_recover_as_the_original_character():
    assert repair_unicode("\ud83d\ude00") == "😀"
