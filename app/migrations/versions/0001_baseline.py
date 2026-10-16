"""Baseline: the schema as it was when ResuApply moved to Alembic.

Revision ID: 0001
Revises:
Create Date: 2026-10-16

A new database gets every table. A database made before Alembic (it has tables but no
``alembic_version``) was kept up to date by an additive upgrade at startup, so it can lack
columns added since it was created, such as ``applications.resume_layout``, ``cover_letter`` and
``cover_letter_draft``. This revision adopts it: a missing table is created, a missing column is
added (each one here is nullable or has a server default, so existing rows stay valid) and a
missing index is created. Nothing that exists is renamed, retyped, rebuilt or dropped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The schema of this revision, frozen: later model changes belong in later revisions.
schema = sa.MetaData()

sa.Table(
    "candidates", schema,
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("profile", sa.JSON(), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("id_counters", sa.JSON(), nullable=False),
    sa.Column("sample", sa.Boolean(), server_default="0", nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
sa.Table(
    "jobs", schema,
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("company", sa.String(length=200), nullable=False),
    sa.Column("title", sa.String(length=200), nullable=False),
    sa.Column("location", sa.String(length=200), nullable=False),
    sa.Column("url", sa.String(length=2000), nullable=False),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("requirements", sa.JSON(), nullable=False),
    sa.Column("requirement_counter", sa.Integer(), server_default="0", nullable=False),
    sa.Column("evidence", sa.JSON(), nullable=False),
    sa.Column("overrides", sa.JSON(), nullable=False),
    sa.Column("questions", sa.JSON(), nullable=False),
    sa.Column("question_counter", sa.Integer(), server_default="0", nullable=False),
    sa.Column("posting_import", sa.JSON(), nullable=True),
    sa.Column("sample", sa.Boolean(), server_default="0", nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint("id"),
)
sa.Table(
    "ai_runs", schema,
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("operation", sa.String(length=40), nullable=False),
    sa.Column("cache_key", sa.String(length=64), nullable=False),
    sa.Column("job_id", sa.Integer(), nullable=True),
    sa.Column("model", sa.String(length=200), nullable=False),
    sa.Column("prompt_revision", sa.String(length=40), nullable=False),
    sa.Column("input_revisions", sa.JSON(), nullable=False),
    sa.Column("result", sa.JSON(), nullable=False),
    sa.Column("usage", sa.JSON(), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("cache_key"),
    sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    sa.Index("ix_ai_runs_job_id", "job_id"),
)
sa.Table(
    "answer_bank", schema,
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("question", sa.Text(), nullable=False),
    sa.Column("answer", sa.Text(), nullable=False),
    sa.Column("category", sa.String(length=40), nullable=False),
    sa.Column("sources", sa.JSON(), nullable=False),
    sa.Column("source_job_id", sa.Integer(), nullable=True),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(["source_job_id"], ["jobs.id"], ondelete="SET NULL"),
    sa.PrimaryKeyConstraint("id"),
    sqlite_autoincrement=True,
)
sa.Table(
    "applications", schema,
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("candidate_id", sa.Integer(), nullable=True),
    sa.Column("job_id", sa.Integer(), nullable=False),
    sa.Column("review_state", sa.String(length=20), nullable=False),
    sa.Column("tracking_status", sa.String(length=20), nullable=False),
    sa.Column("status_history", sa.JSON(), nullable=False),
    sa.Column("notes", sa.JSON(), nullable=False),
    sa.Column("applied_on", sa.Date(), nullable=True),
    sa.Column("follow_up_after", sa.Date(), nullable=True),
    sa.Column("current_proposal", sa.JSON(), nullable=True),
    sa.Column("accepted_package", sa.JSON(), nullable=True),
    sa.Column("resume_layout", sa.String(length=32), server_default="classic", nullable=False),
    sa.Column("answers", sa.JSON(), nullable=False),
    sa.Column("answer_drafts", sa.JSON(), server_default="[]", nullable=False),
    sa.Column("cover_letter", sa.JSON(), nullable=True),
    sa.Column("cover_letter_draft", sa.JSON(), nullable=True),
    sa.Column("approval", sa.JSON(), nullable=True),
    sa.Column("approval_rules", sa.Integer(), server_default="1", nullable=False),
    sa.Column("approval_rules_for", sa.String(length=64), nullable=True),
    sa.Column("submitted_snapshots", sa.JSON(), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("updated_at", sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("job_id"),
    sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
)
sa.Table(
    "outbound_approvals", schema,
    sa.Column("id", sa.Integer(), nullable=False),
    sa.Column("candidate_id", sa.Integer(), nullable=False),
    sa.Column("base_hash", sa.String(length=32), nullable=False),
    sa.Column("excluded", sa.JSON(), nullable=False),
    sa.Column("edits", sa.JSON(), nullable=False),
    sa.Column("profile_revision", sa.Integer(), nullable=False),
    sa.Column("approved_at", sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint("id"),
    sa.UniqueConstraint("candidate_id"),
    sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for table in schema.sorted_tables:
        if table.name not in existing_tables:
            table.create(bind)
            continue
        columns = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in columns:
                continue
            if not column.nullable and column.server_default is None:
                raise RuntimeError(f"Cannot add {table.name}.{column.name} to an existing database: "
                                   "it has no server default.")
            # SQLite adds a column in place; batch mode would rebuild the table.
            default = column.server_default.arg if column.server_default is not None else None
            op.add_column(table.name, sa.Column(column.name, column.type, nullable=column.nullable,
                                                server_default=default))
        indexes = {i["name"] for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in indexes:
                op.create_index(index.name, table.name, [c.name for c in index.columns], unique=index.unique)


def downgrade() -> None:
    raise RuntimeError("The baseline can't be undone: it would delete every table. "
                       "Restore the copy saved before the upgrade instead (resuapply.db.before-<revision>.bak).")
