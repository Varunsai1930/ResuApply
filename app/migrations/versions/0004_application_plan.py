"""Application priorities and optional submission deadlines."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('applications')}
    if 'priority' not in columns:
        op.add_column('applications', sa.Column('priority', sa.String(16), nullable=False, server_default='normal'))
    if 'deadline' not in columns:
        op.add_column('applications', sa.Column('deadline', sa.Date(), nullable=True))


def downgrade():
    with op.batch_alter_table('applications') as batch:
        batch.drop_column('deadline')
        batch.drop_column('priority')
