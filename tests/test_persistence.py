"""Milestone 1 acceptance gate: save data, restart, reopen it; IDs survive edits."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.db import init_db, make_engine, make_session_factory
from app.main import create_app
from app.services import profile as profile_service
from tests.conftest import BASE_URL, SAMPLE_JOB
from tests.test_routes import client_profile, create_job, profile_form, review_and_save


def test_database_lives_in_the_data_folder_with_all_tables(settings):
    with TestClient(create_app(settings), base_url=BASE_URL):
        pass
    assert settings.database_path.exists()
    assert settings.database_path.parent == settings.resolved_data_dir
    engine = make_engine(settings.database_url)
    assert set(inspect(engine).get_table_names()) == {
        "candidates", "jobs", "applications", "answer_bank", "ai_runs", "outbound_approvals",
    }
    engine.dispose()


def test_everything_survives_a_restart(settings, sample_profile):
    # First run: create a profile, edit it, add a job, change its status, add a note.
    with TestClient(create_app(settings), base_url=BASE_URL) as first:
        review_and_save(first, profile_form(sample_profile))
        profile = client_profile(first)
        profile["experience"][0]["bullets"].pop(0)
        profile["experience"][0]["bullets"].append({"text": "Mentored a new intern"})
        review_and_save(first, profile_form(profile))
        job_id = create_job(first)
        first.post(f"/jobs/{job_id}/status", data={"status": "applied", "on": "2026-02-01", "note": "Submitted myself"})
        first.post(f"/jobs/{job_id}/notes", data={"text": "Recruiter: Sam Example"})
        before_profile = first.get("/profile").text
        before_job = first.get(f"/jobs/{job_id}").text
    # Leaving the TestClient context runs shutdown, which disposes the engine.

    # Second run: a brand-new app and engine on the same database file.
    with TestClient(create_app(settings), base_url=BASE_URL) as second:
        assert second.get("/profile").text == before_profile
        assert second.get(f"/jobs/{job_id}").text == before_job
        page = second.get(f"/jobs/{job_id}").text
        assert "Submitted myself" in page and "Recruiter: Sam Example" in page and "2026-02-01" in page

        # IDs keep surviving edits after the restart, and deleted ones are never reused.
        profile = client_profile(second)
        profile["experience"][0]["bullets"].append({"text": "Documented the reporting API"})
        review_and_save(second, profile_form(profile))

    engine = make_engine(settings.database_url)
    init_db(engine)
    with make_session_factory(engine)() as session:
        candidate = profile_service.get_candidate(session)
        assert candidate.revision == 3
        bullets = candidate.profile.experience[0].bullets
        assert [b.id for b in bullets] == ["exp-1-b2", "exp-1-b3", "exp-1-b4", "exp-1-b5"]
        assert bullets[2].text == "Mentored a new intern"
        assert candidate.id_counters["exp-1-b"] == 5
    engine.dispose()


def test_job_text_is_unchanged_after_restart(settings):
    with TestClient(create_app(settings), base_url=BASE_URL) as first:
        job_id = create_job(first)
    with TestClient(create_app(settings), base_url=BASE_URL) as second:
        edit = second.get(f"/jobs/{job_id}/edit").text
    assert SAMPLE_JOB["description"] in edit.replace("&#39;", "'")
