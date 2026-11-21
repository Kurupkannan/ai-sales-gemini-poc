"""Persist the post-call Gemini screening result."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0006'
down_revision = '0005'
branch_labels = depends_on = None


def upgrade():
    op.add_column('call_sessions', sa.Column('result_summary', JSONB(), nullable=True), schema='app')


def downgrade():
    op.drop_column('call_sessions', 'result_summary', schema='app')
