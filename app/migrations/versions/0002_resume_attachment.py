"""Add applications.resume_attachment for Greenhouse autofill.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16

The resume PDF the user attached to the approved package (file name, SHA-256, size, pages, the
package hash and when). Nullable, so existing rows are unchanged and an application without an
attachment needs nothing.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("applications")}
    if "resume_attachment" not in columns:  # a database that already had it from an earlier test build
        op.add_column("applications", sa.Column("resume_attachment", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("applications") as batch:
        batch.drop_column("resume_attachment")
