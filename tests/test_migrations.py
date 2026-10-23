"""Alembic migrations: new databases, databases made before migrations, and failed upgrades.

All data is synthetic. An "old-shaped" database is made the way an earlier release made it (every
table, but no ``alembic_version`` and without the columns added since), then the app is started.
"""

from __future__ import annotations

import copy
import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app import db as db_module
from app.config import Settings
from app.db import Base, head_revision, init_db, make_engine
from app.main import create_app
from app.models import Job
from app.services import jobs, profile, resume
from tests.conftest import BASE_URL, NO_NETWORK
from tests.synthetic import DEMO_JOB, SAMPLE_PROFILE

# Columns added after the first releases; an older database has none of them.
LATER_COLUMNS = {
    "applications": ["resume_layout", "cover_letter", "cover_letter_draft", "answer_drafts", "approval_rules",
                     "approval_rules_for", "follow_up_after", "resume_versions", "priority", "deadline"],
    "jobs": ["posting_import", "sample"],
    "candidates": ["sample"],
}


def _version(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _old_database(settings: Settings, *, drop_tables: tuple[str, ...] = ()) -> dict:
    """Make a database the old additive upgrade would have kept, with a profile, a job and an accepted resume."""
    settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        with Session(engine) as session:
            profile.save(session, copy.deepcopy(SAMPLE_PROFILE))
            job = jobs.create(session, jobs.clean_input(**DEMO_JOB))
            candidate = profile.get_candidate(session)
            record = resume.propose(session, job, candidate, resume.profile_draft(candidate.profile))
            resume.accept(session, job, candidate, resume.proposal_token(record))
            job_id = job.id
        with engine.begin() as conn:
            kept = {
                "job_id": job_id,
                "description": conn.execute(text("SELECT description FROM jobs")).scalar_one(),
                "profile": conn.execute(text("SELECT profile FROM candidates")).scalar_one(),
                "package": conn.execute(text("SELECT accepted_package FROM applications")).scalar_one(),
            }
            conn.execute(text("DROP TABLE alembic_version"))
            for table in drop_tables:
                conn.execute(text(f'DROP TABLE "{table}"'))
            for table, columns in LATER_COLUMNS.items():
                for column in columns:
                    conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))
    finally:
        engine.dispose()
    return kept


def _start(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, greenhouse_transport=NO_NETWORK), base_url=BASE_URL)


def test_a_new_database_is_created_at_the_latest_revision_and_matches_the_models(settings):
    settings.resolved_data_dir.mkdir(parents=True)
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        assert _version(engine) == head_revision()
        with engine.connect() as conn:
            # An empty difference also means no model change is missing its migration.
            assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []
        init_db(engine)  # nothing to do the second time
        assert _version(engine) == head_revision()
    finally:
        engine.dispose()
    # Nothing existed to copy, so no backup is made.
    assert not list(settings.resolved_data_dir.glob("*.bak"))


def test_the_migrations_alone_build_exactly_the_models_schema(settings):
    """New databases are created from the models and stamped, so check the migrations build the same."""
    settings.resolved_data_dir.mkdir(parents=True)
    migrator = db_module.migration_engine(settings.database_url)
    try:
        with migrator.connect() as conn:
            with conn.begin():
                command.upgrade(db_module._alembic_config(conn), "head")
            assert MigrationContext.configure(conn).get_current_revision() == head_revision()
            assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []
    finally:
        migrator.dispose()


def test_an_old_database_is_adopted_on_startup_and_keeps_its_data(settings):
    kept = _old_database(settings)
    with _start(settings) as client:
        engine = client.app.state.engine
        assert _version(engine) == head_revision()
        for table, columns in LATER_COLUMNS.items():
            assert set(columns) <= _columns(engine, table)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT description FROM jobs")).scalar_one() == kept["description"]
            assert conn.execute(text("SELECT profile FROM candidates")).scalar_one() == kept["profile"]
            assert conn.execute(text("SELECT accepted_package FROM applications")).scalar_one() == kept["package"]
            row = conn.execute(text("SELECT resume_layout, cover_letter, cover_letter_draft, answer_drafts, "
                                    "approval_rules FROM applications")).one()
            assert tuple(row) == ("classic", None, None, "[]", 1)
        with client.app.state.session_factory() as session:
            job = session.get(Job, kept["job_id"])
            assert job.application.accepted_package is not None and job.application.cover_letter is None
        page = client.get(f"/jobs/{kept['job_id']}?step=resume")
        assert page.status_code == 200 and DEMO_JOB["title"] in page.text

    # A copy of the database as it was is kept next to it, unchanged.
    backup = settings.database_path.with_name(f"resuapply.db.before-{head_revision()}.bak")
    with sqlite3.connect(backup) as copy_db:
        tables = {name for (name,) in copy_db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "alembic_version" not in tables and "applications" in tables
        old_columns = {row[1] for row in copy_db.execute("PRAGMA table_info(applications)")}
        assert "resume_layout" not in old_columns
        assert copy_db.execute("SELECT accepted_package FROM applications").fetchone()[0] == kept["package"]
    copy_db.close()

    # Starting again changes nothing and makes no new copy.
    stamp = backup.stat().st_mtime_ns
    with _start(settings) as client:
        assert _version(client.app.state.engine) == head_revision()
        assert client.get(f"/jobs/{kept['job_id']}").status_code == 200
    assert backup.stat().st_mtime_ns == stamp
    assert [p.name for p in settings.resolved_data_dir.glob("*.bak")] == [backup.name]


def test_an_old_database_missing_whole_tables_gets_them(settings):
    kept = _old_database(settings, drop_tables=("outbound_approvals", "answer_bank"))
    with _start(settings) as client:
        engine = client.app.state.engine
        assert {"outbound_approvals", "answer_bank"} <= set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata) == []
            assert conn.execute(text("SELECT accepted_package FROM applications")).scalar_one() == kept["package"]


def test_an_old_database_missing_the_ai_run_index_gets_it(settings):
    _old_database(settings)
    engine = make_engine(settings.database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_ai_runs_job_id"))
        init_db(engine)
        assert "ix_ai_runs_job_id" in {i["name"] for i in inspect(engine).get_indexes("ai_runs")}
    finally:
        engine.dispose()


def test_a_failed_upgrade_rolls_back_completely(settings, monkeypatch):
    kept = _old_database(settings)
    real_upgrade = command.upgrade

    def upgrade_then_fail(config, revision):
        real_upgrade(config, revision)  # every change is made, then something goes wrong
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(db_module.command, "upgrade", upgrade_then_fail)
    engine = make_engine(settings.database_url)
    try:
        with pytest.raises(RuntimeError, match="simulated failure"):
            init_db(engine)
        assert "alembic_version" not in inspect(engine).get_table_names()
        assert "resume_layout" not in _columns(engine, "applications")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT accepted_package FROM applications")).scalar_one() == kept["package"]
        monkeypatch.setattr(db_module.command, "upgrade", real_upgrade)
        init_db(engine)  # the next start upgrades normally
        assert _version(engine) == head_revision() and "resume_layout" in _columns(engine, "applications")
    finally:
        engine.dispose()


def test_an_upgrade_that_would_break_references_is_refused(settings, monkeypatch):
    _old_database(settings)
    real_upgrade = command.upgrade

    def upgrade_and_orphan(config, revision):
        real_upgrade(config, revision)
        config.attributes["connection"].exec_driver_sql("DELETE FROM jobs")  # foreign keys are off here

    monkeypatch.setattr(db_module.command, "upgrade", upgrade_and_orphan)
    engine = make_engine(settings.database_url)
    try:
        with pytest.raises(RuntimeError, match="rolled back"):
            init_db(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
        assert "alembic_version" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_the_baseline_cannot_be_downgraded(settings):
    settings.resolved_data_dir.mkdir(parents=True)
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        migrator = db_module.migration_engine(settings.database_url)
        try:
            with migrator.connect() as conn:
                # Later revisions step down first; the baseline then refuses, and the whole transaction rolls back.
                with pytest.raises(RuntimeError, match="can't be undone"):
                    with conn.begin():
                        command.downgrade(db_module._alembic_config(conn), "base")
        finally:
            migrator.dispose()
        assert _version(engine) == head_revision()
        assert "applications" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_an_existing_backup_from_another_database_is_kept_and_this_one_is_copied_too(settings):
    kept = _old_database(settings)
    stale = settings.database_path.with_name(f"resuapply.db.before-{head_revision()}.bak")
    stale.write_bytes(b"a copy of some other database")
    with _start(settings) as client:
        assert _version(client.app.state.engine) == head_revision()
    assert stale.read_bytes() == b"a copy of some other database"  # never overwritten
    fresh = [p for p in settings.resolved_data_dir.glob("*.bak") if p != stale]
    assert len(fresh) == 1 and fresh[0].name.startswith(f"resuapply.db.before-{head_revision()}.")
    with closing(sqlite3.connect(fresh[0])) as copy_db:
        assert copy_db.execute("SELECT accepted_package FROM applications").fetchone()[0] == kept["package"]


def test_an_up_to_date_database_starts_while_another_connection_holds_the_write_lock(settings):
    import time

    settings.resolved_data_dir.mkdir(parents=True)
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as writer:
            writer.execute("BEGIN IMMEDIATE")  # e.g. another ResuApply or a long write
            began = time.monotonic()
            init_db(engine)  # reads the revision and stops; it never waits for the lock
            assert time.monotonic() - began < 2
            writer.execute("ROLLBACK")
    finally:
        engine.dispose()


def test_a_database_at_the_baseline_gains_the_resume_attachment_column(settings):
    settings.resolved_data_dir.mkdir(parents=True)
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        with Session(engine) as session:
            profile.save(session, copy.deepcopy(SAMPLE_PROFILE))
            jobs.create(session, jobs.clean_input(**DEMO_JOB))
        with engine.begin() as conn:  # as revision 0001 left it, before Greenhouse autofill
            conn.execute(text("ALTER TABLE applications DROP COLUMN resume_attachment"))
            conn.execute(text("UPDATE alembic_version SET version_num = '0001'"))
        init_db(engine)
        assert _version(engine) == head_revision()
        assert "resume_attachment" in _columns(engine, "applications")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT resume_attachment FROM applications")).scalar_one() is None
            assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
    finally:
        engine.dispose()
    assert settings.database_path.with_name(f"resuapply.db.before-{head_revision()}.bak").exists()
