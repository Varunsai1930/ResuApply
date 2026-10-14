"""Legacy snapshot profiles are readable without changing the facts frozen in storage."""

import copy
import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import StatementError

from app.main import create_app
from app.schemas.package import SubmittedSnapshot
from app.services import jobs, profile, tracking
from app.unicode_text import has_invalid_unicode
from tests.conftest import BASE_URL, NO_NETWORK
from tests.synthetic import DEMO_JOB
from tests.test_package import accept_resume, approve, record_applied


def damaged_history(settings, sample_profile, notes, *, stored_rules=False):
    with TestClient(create_app(settings, greenhouse_transport=NO_NETWORK), base_url=BASE_URL) as first:
        with first.app.state.session_factory() as session:
            candidate = profile.save(session, sample_profile).candidate
            job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
            accept_resume(session, job, candidate)
            approve(session, job, candidate)
            record_applied(session, job, candidate)
            snapshots = json.loads(session.scalar(text("SELECT submitted_snapshots FROM applications")))
            snapshots[0]["profile"]["availability"]["notes"] = notes
            if stored_rules:
                snapshots[0]["approval"]["rules"] = 2
            raw = json.dumps(snapshots)
            session.execute(text("UPDATE applications SET submitted_snapshots = :snapshots"), {"snapshots": raw})
            session.commit()
            return job.id, raw, json.loads(raw)


@pytest.mark.parametrize("stored_rules", [False, True])
@pytest.mark.parametrize("notes, readable", [
    ("Available \ud800soon", "Available \ufffdsoon"),
    ("Available \udfffsoon", "Available \ufffdsoon"),
    ("Available \ud83d\ude00", "Available 😀"),
])
def test_restart_recovers_snapshot_display_and_preserves_frozen_storage(
    settings, sample_profile, stored_rules, notes, readable,
):
    job_id, raw, original = damaged_history(settings, sample_profile, notes, stored_rules=stored_rules)
    with TestClient(create_app(settings, greenhouse_transport=NO_NETWORK), base_url=BASE_URL) as restarted:
        for endpoint in ("/profile", f"/jobs/{job_id}", f"/jobs/{job_id}/snapshots/1",
                         f"/jobs/{job_id}/snapshots/1/resume"):
            assert restarted.get(endpoint).status_code == 200, endpoint
        with restarted.app.state.session_factory() as session:
            candidate = profile.get_candidate(session)
            snapshot = jobs.get(session, job_id).application.submitted_snapshots[0]
            assert not candidate.profile.unicode_recovered
            assert snapshot.profile.unicode_recovered == has_invalid_unicode(original[0]["profile"])
            assert snapshot.profile.availability.notes == readable
            assert not has_invalid_unicode(snapshot.model_dump())
            snapshot.model_dump_json().encode("utf-8")
            stored = session.scalar(text("SELECT submitted_snapshots FROM applications"))
            expected = copy.deepcopy(original)
            if stored_rules:
                del expected[0]["approval"]["rules"]
            else:
                assert stored == raw
            assert json.loads(stored) == expected


def test_appending_a_snapshot_preserves_the_original_legacy_profile(settings, sample_profile):
    job_id, _, original = damaged_history(settings, sample_profile, "Available \ud800soon", stored_rules=True)
    with TestClient(create_app(settings, greenhouse_transport=NO_NETWORK), base_url=BASE_URL) as restarted:
        with restarted.app.state.session_factory() as session:
            candidate = profile.get_candidate(session)
            job = jobs.get(session, job_id)
            tracking.change_status(session, job.application, "withdrawn")
            second = record_applied(session, job, candidate)
            assert second.id == 2
            stored = json.loads(session.scalar(text("SELECT submitted_snapshots FROM applications")))
            assert stored[0]["profile"] == original[0]["profile"]
            assert "rules" not in stored[0]["approval"]
            assert stored[1]["profile"] == candidate.profile.model_dump(mode="json")
            session.expire_all()
            snapshots = jobs.get(session, job_id).application.submitted_snapshots
            assert snapshots[0].profile.availability.notes == "Available \ufffdsoon"
            assert not snapshots[1].profile.unicode_recovered


def test_new_snapshot_profiles_still_reject_damaged_unicode(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    accept_resume(session, job, candidate)
    approve(session, job, candidate)
    snapshot = record_applied(session, job, candidate)
    data = snapshot.model_dump()
    data["profile"]["availability"]["notes"] = "\ud800"
    with pytest.raises(ValidationError, match="unreadable Unicode"):
        SubmittedSnapshot.model_validate(data)
    job.application.submitted_snapshots = [data]
    with pytest.raises(StatementError, match="unreadable Unicode"):
        session.commit()
    session.rollback()
    assert not has_invalid_unicode(job.application.submitted_snapshots[0].profile)


def test_mutated_snapshot_profiles_cannot_bypass_column_validation(session, sample_profile):
    candidate = profile.save(session, sample_profile).candidate
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    accept_resume(session, job, candidate)
    approve(session, job, candidate)
    original = record_applied(session, job, candidate)
    changed = original.model_copy(deep=True)
    changed.profile.availability.notes = "\ud800"
    job.application.submitted_snapshots = [changed]
    with pytest.raises(StatementError, match="unreadable Unicode"):
        session.commit()
    session.rollback()
    assert job.application.submitted_snapshots[0].profile.availability.notes == sample_profile["availability"]["notes"]
