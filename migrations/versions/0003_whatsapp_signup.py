"""Private Embedded Signup connections and webhook receipt metadata."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = depends_on = None


def record_columns():
    return [sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))]


def upgrade():
    op.create_table("whatsapp_connections", *record_columns(),
        sa.Column("workspace", sa.String(40), nullable=False, server_default="private-pilot"),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("token_ciphertext", sa.Text()),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("scopes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("candidate_wabas", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("waba_id", sa.String(40)),
        sa.Column("phone_number_id", sa.String(40), unique=True),
        sa.Column("display_phone_number", sa.String(80)),
        sa.Column("verified_name", sa.String(200)),
        sa.Column("subscribed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("registered", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_error", sa.String(300)),
        sa.Column("operation_until", sa.DateTime(timezone=True)),
        sa.Column("operation_id", sa.String(40)),
        sa.Column("last_webhook_at", sa.DateTime(timezone=True)), schema="app")
    op.create_index("ix_whatsapp_connections_session_hash", "whatsapp_connections", ["session_hash"], schema="app")
    op.create_table("whatsapp_webhook_receipts", *record_columns(),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("entry_count", sa.Integer(), nullable=False), schema="app")
    op.execute("REVOKE ALL ON app.whatsapp_connections, app.whatsapp_webhook_receipts FROM PUBLIC, anon, authenticated, service_role")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_sales_runtime') THEN
        GRANT SELECT, INSERT, UPDATE ON app.whatsapp_connections TO ai_sales_runtime;
        GRANT SELECT, INSERT ON app.whatsapp_webhook_receipts TO ai_sales_runtime;
      END IF;
    END $$""")


def downgrade():
    op.drop_table("whatsapp_webhook_receipts", schema="app")
    op.drop_table("whatsapp_connections", schema="app")
