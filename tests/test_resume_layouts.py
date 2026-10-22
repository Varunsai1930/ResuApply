"""Layout validation, upgrades and identical accepted content in each print style."""

from __future__ import annotations

import copy
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.db import init_db, make_engine
from app.models import Application, Job
from app.services import jobs, profile, resume
from app.services.resume_layouts import LAYOUTS, get_layout, layout_key
from app.templating import templates
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE


@pytest.mark.parametrize("key", ["classic", "modern", "compact"])
def test_supported_layouts_have_stable_keys_and_descriptions(key):
    choice = get_layout(key)
    assert choice.key == key
    assert choice in LAYOUTS
    assert choice.label and choice.description


@pytest.mark.parametrize("value", ["", "Classic", " CLASSIC ", "two-column", "../modern", None, [], {}])
def test_unknown_request_layouts_are_refused(value):
    with pytest.raises(ValueError, match="Choose Classic, Modern or Compact"):
        get_layout(value)


@pytest.mark.parametrize("value", [None, "", "obsolete", "CLASSIC"])
def test_legacy_missing_or_unknown_layout_is_displayed_as_classic(value):
    assert layout_key(SimpleNamespace(resume_layout=value)) == "classic"
    assert layout_key(SimpleNamespace()) == "classic"


def test_layout_defaults_and_choices_persist_without_rewriting_content(session):
    profile.save(session, copy.deepcopy(SAMPLE_PROFILE))
    job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
    candidate = profile.get_candidate(session)
    assert job.application.resume_layout == "classic"
    record = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
    accepted = resume.accept(session, job, candidate, resume.proposal_token(record))
    job.application.resume_layout = "modern"
    session.commit()
    with Session(session.bind) as reopened:
        loaded = reopened.get(Job, job.id)
        assert layout_key(loaded.application) == "modern"
        assert loaded.application.accepted_package == accepted


def test_additive_layout_upgrade_preserves_existing_package_and_is_idempotent(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    try:
        init_db(engine)
        with Session(engine) as db:
            profile.save(db, copy.deepcopy(SAMPLE_PROFILE))
            job = jobs.create(db, jobs.clean_input(**DEMO_JOB))
            candidate = profile.get_candidate(db)
            record = resume.propose(db, job, candidate, resume.profile_draft(candidate.profile))
            resume.accept(db, job, candidate, resume.proposal_token(record))
            job_id = job.id
        with engine.begin() as conn:
            before = conn.execute(text("SELECT accepted_package FROM applications")).scalar_one()
            # Emulate a pre-layout database without touching the accepted package.
            conn.execute(text("ALTER TABLE applications DROP COLUMN resume_layout"))
            conn.execute(text("DROP TABLE alembic_version"))  # made before migrations
        init_db(engine)
        init_db(engine)
        column = next(c for c in inspect(engine).get_columns("applications") if c["name"] == "resume_layout")
        assert column["nullable"] is False
        with engine.connect() as conn:
            assert conn.execute(text("SELECT resume_layout FROM applications")).scalar_one() == "classic"
            assert conn.execute(text("SELECT accepted_package FROM applications")).scalar_one() == before
        with Session(engine) as db:
            assert db.get(Job, job_id).application.resume_layout == "classic"
            # Omit the new field using a direct insert, to exercise the database default.
            another = jobs.create(db, jobs.clean_input(**(DEMO_JOB | {"title": "Another role"})))
            db.execute(text("DELETE FROM applications WHERE job_id = :job_id"), {"job_id": another.id})
            db.execute(text("INSERT INTO applications (job_id, review_state, tracking_status, status_history, notes, answers, submitted_snapshots, created_at, updated_at) VALUES (:job_id, 'draft', 'saved', '[]', '[]', '[]', '[]', '2026-01-01', '2026-01-01')"), {"job_id": another.id})
            db.commit()
            assert db.execute(text("SELECT resume_layout FROM applications WHERE job_id = :job_id"), {"job_id": another.id}).scalar_one() == "classic"
    finally:
        engine.dispose()


def test_each_layout_keeps_all_selected_content_and_escapes_untrusted_text(session):
    raw = copy.deepcopy(SAMPLE_PROFILE)
    raw["contact"]["name"] = 'Zoë <script>alert("name")</script> Example'
    raw["experience"][0]["bullets"][0] = '<img src=x onerror="alert(1)"> Built a local parser.'
    raw["contact"]["links"]["github"] = "https://example.test/" + "long-link" * 25
    profile.save(session, raw)
    candidate = profile.get_candidate(session)
    rendered = resume.render_context(candidate.profile, resume.profile_draft(candidate.profile))
    template = templates.env.get_template("_resume_document.html")
    documents = []
    for choice in LAYOUTS:
        output = template.render(rendered=rendered, selected_layout=choice.key)
        assert f'resume-layout-{choice.key}' in output
        assert "Zoë &lt;script&gt;" in output
        assert "&lt;img src=x" in output
        assert "<script>" not in output and "<img " not in output
        assert "Technologies: Python, Flask, PostgreSQL" in output
        assert "Technologies: Python, SQLite" in output
        assert raw["contact"]["links"]["github"] in output
        assert "Coursework: Data Structures, Operating Systems, Databases" in output
        assert "Example State University" in output
        documents.append(re.sub(r"resume-layout-(classic|modern|compact)", "resume-layout-CHOICE", output))
    assert documents[0] == documents[1] == documents[2]
    assert "resume-layout-classic" in template.render(rendered=rendered)


def test_print_layout_selector_identifies_the_reviewed_package(session):
    profile.save(session, copy.deepcopy(SAMPLE_PROFILE))
    candidate = profile.get_candidate(session)
    rendered = resume.render_context(candidate.profile, resume.profile_draft(candidate.profile))
    output = templates.env.get_template("resume_print.html").render(
        rendered=rendered, job=SimpleNamespace(id=17), selected_layout="compact",
        layout_choices=LAYOUTS, layout_token="reviewed-package-token",
    )
    assert 'action="/jobs/17/resume/layout"' in output
    assert 'name="package_token" value="reviewed-package-token"' in output
    assert '<input type="radio" name="layout" value="compact" checked>' in output
    assert output.count('type="radio" name="layout"') == 3
    assert "Changing the layout requires package review again." in output
