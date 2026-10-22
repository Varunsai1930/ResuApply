"""Keep the most recent accepted resume versions for local comparison and recovery."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("applications")}
    if "resume_versions" not in columns:
        op.add_column("applications", sa.Column("resume_versions", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    with op.batch_alter_table("applications") as batch:
        batch.drop_column("resume_versions")
