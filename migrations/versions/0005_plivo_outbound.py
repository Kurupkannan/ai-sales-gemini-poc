"""Plivo outbound call correlation and recoverable media grants."""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    # Preserve the provenance of existing calls; new rows default to Plivo.
    op.add_column('call_sessions', sa.Column('provider', sa.String(20), server_default='exotel', nullable=False), schema='app')
    op.alter_column('call_sessions', 'provider', server_default='plivo', schema='app')
    for column in [
        sa.Column('provider_account_id', sa.String(100), server_default='', nullable=False),
        sa.Column('provider_request_id', sa.String(200)),
        sa.Column('provider_status', sa.String(100)),
        sa.Column('bill_duration', sa.Integer()),
    ]:
        op.add_column('call_sessions', column, schema='app')
    op.drop_constraint('uq_call_sessions_idempotency_key', 'call_sessions', schema='app', type_='unique')
    op.drop_constraint('uq_call_sessions_provider_call_id', 'call_sessions', schema='app', type_='unique')
    op.create_unique_constraint('uq_call_sessions_organization_id', 'call_sessions', ['organization_id', 'idempotency_key'], schema='app')
    op.create_unique_constraint('uq_call_sessions_provider', 'call_sessions', ['provider', 'provider_account_id', 'provider_call_id'], schema='app')
    op.add_column('outbound_authorizations', sa.Column('provider', sa.String(20), server_default='exotel', nullable=False), schema='app')
    op.alter_column('outbound_authorizations', 'provider', server_default='plivo', schema='app')
    for column in [
        sa.Column('provider_account_id', sa.String(100)),
        sa.Column('provider_request_id', sa.String(200)),
        sa.Column('call_session_id', sa.Uuid()),
        sa.Column('from_number', sa.String(16)), sa.Column('to_number', sa.String(16)),
        sa.Column('token_ciphertext', sa.Text()),
        sa.Column('answered_at', sa.DateTime(timezone=True)),
        sa.Column('stream_id', sa.String(200)),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True)),
        sa.Column('cleanup_needed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('reconcile_after', sa.DateTime(timezone=True)),
        sa.Column('end_reason', sa.String(200)),
    ]:
        op.add_column('outbound_authorizations', column, schema='app')
    op.create_foreign_key('fk_outbound_authorizations_call_session_id_call_sessions', 'outbound_authorizations', 'call_sessions', ['call_session_id'], ['id'], source_schema='app', referent_schema='app')
    op.create_foreign_key('fk_outbound_authorizations_org_call_session_id', 'outbound_authorizations', 'call_sessions', ['organization_id', 'call_session_id'], ['organization_id', 'id'], source_schema='app', referent_schema='app')
    op.create_unique_constraint('uq_outbound_authorizations_call_session_id', 'outbound_authorizations', ['call_session_id'], schema='app')
    op.create_index('ix_outbound_authorizations_reconcile_after', 'outbound_authorizations', ['reconcile_after'], schema='app')


def downgrade():
    raise RuntimeError('Retain call history and callback correlation; restore a reviewed backup to undo this migration.')
