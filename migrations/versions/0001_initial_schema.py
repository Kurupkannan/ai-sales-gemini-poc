"""Initial lead, consent and call schema with restricted runtime grants.

Revision ID: 0001
Revises: None
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "leads",
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("preferred_language", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="new", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('new', 'contacted', 'qualified', 'unqualified', 'closed')",
            name=op.f("ck_leads_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
        schema="app",
    )
    op.create_index(
        op.f("ix_leads_status"), "leads", ["status"], unique=False, schema="app"
    )
    op.create_table(
        "lead_contacts",
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("phone_number", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column(
            "verification_state",
            sa.String(length=20),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('voice', 'whatsapp')", name=op.f("ck_lead_contacts_channel")
        ),
        sa.CheckConstraint(
            "phone_number ~ '^\\+[1-9][0-9]{1,14}$'", name=op.f("ck_lead_contacts_e164")
        ),
        sa.CheckConstraint(
            "verification_state IN ('unknown', 'verified', 'invalid')",
            name=op.f("ck_lead_contacts_verification"),
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["app.leads.id"], name=op.f("fk_lead_contacts_lead_id_leads")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lead_contacts")),
        sa.UniqueConstraint("id", "lead_id", name=op.f("uq_lead_contacts_id")),
        sa.UniqueConstraint(
            "lead_id", "phone_number", "channel", name=op.f("uq_lead_contacts_lead_id")
        ),
        schema="app",
    )
    op.create_index(
        op.f("ix_lead_contacts_lead_id"),
        "lead_contacts",
        ["lead_id"],
        unique=False,
        schema="app",
    )
    op.create_index(
        op.f("ix_lead_contacts_phone_number"),
        "lead_contacts",
        ["phone_number"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "consent_events",
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('granted', 'revoked')", name=op.f("ck_consent_events_action")
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["app.lead_contacts.id"],
            name=op.f("fk_consent_events_contact_id_lead_contacts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_events")),
        schema="app",
    )
    op.create_index(
        op.f("ix_consent_events_contact_id"),
        "consent_events",
        ["contact_id"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "contact_preferences",
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("suppressed", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "reason",
            sa.String(length=200),
            server_default="consent_unknown",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["app.lead_contacts.id"],
            name=op.f("fk_contact_preferences_contact_id_lead_contacts"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_preferences")),
        sa.UniqueConstraint(
            "contact_id", name=op.f("uq_contact_preferences_contact_id")
        ),
        schema="app",
    )
    op.create_table(
        "conversations",
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column(
            "automation_mode",
            sa.String(length=20),
            server_default="automated",
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "automation_mode IN ('automated', 'human', 'paused')",
            name=op.f("ck_conversations_automation_mode"),
        ),
        sa.CheckConstraint(
            "channel IN ('voice', 'whatsapp')", name=op.f("ck_conversations_channel")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'completed', 'failed')",
            name=op.f("ck_conversations_status"),
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name=op.f("ck_conversations_time_order"),
        ),
        sa.ForeignKeyConstraint(
            ["contact_id", "lead_id"],
            ["app.lead_contacts.id", "app.lead_contacts.lead_id"],
            name=op.f("fk_conversations_contact_id_lead_contacts"),
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["app.leads.id"], name=op.f("fk_conversations_lead_id_leads")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        schema="app",
    )
    op.create_index(
        op.f("ix_conversations_contact_id"),
        "conversations",
        ["contact_id"],
        unique=False,
        schema="app",
    )
    op.create_index(
        "ix_conversations_lead_created",
        "conversations",
        ["lead_id", "created_at"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "call_sessions",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("provider_call_id", sa.String(length=200), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("stream_id", sa.String(length=200), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column("end_reason", sa.String(length=200), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'initiated', 'active', 'completed', 'failed', 'unknown')",
            name=op.f("ck_call_sessions_status"),
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name=op.f("ck_call_sessions_time_order"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["app.conversations.id"],
            name=op.f("fk_call_sessions_conversation_id_conversations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_sessions")),
        sa.UniqueConstraint("id", "conversation_id", name=op.f("uq_call_sessions_id")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_call_sessions_idempotency_key")
        ),
        sa.UniqueConstraint(
            "provider_call_id", name=op.f("uq_call_sessions_provider_call_id")
        ),
        schema="app",
    )
    op.create_index(
        op.f("ix_call_sessions_conversation_id"),
        "call_sessions",
        ["conversation_id"],
        unique=False,
        schema="app",
    )
    op.create_table(
        "transcript_turns",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("call_session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interrupted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "speaker IN ('customer', 'agent', 'system')",
            name=op.f("ck_transcript_turns_speaker"),
        ),
        sa.CheckConstraint("sequence >= 0", name=op.f("ck_transcript_turns_sequence")),
        sa.ForeignKeyConstraint(
            ["call_session_id", "conversation_id"],
            ["app.call_sessions.id", "app.call_sessions.conversation_id"],
            name=op.f("fk_transcript_turns_call_session_id_call_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["app.conversations.id"],
            name=op.f("fk_transcript_turns_conversation_id_conversations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcript_turns")),
        sa.UniqueConstraint(
            "call_session_id",
            "sequence",
            name=op.f("uq_transcript_turns_call_session_id"),
        ),
        schema="app",
    )
    op.create_index(
        op.f("ix_transcript_turns_call_session_id"),
        "transcript_turns",
        ["call_session_id"],
        unique=False,
        schema="app",
    )
    op.create_index(
        op.f("ix_transcript_turns_conversation_id"),
        "transcript_turns",
        ["conversation_id"],
        unique=False,
        schema="app",
    )

    op.execute(
        "REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC, anon, authenticated, service_role"
    )
    op.execute(
        "REVOKE ALL ON SCHEMA app FROM PUBLIC, anon, authenticated, service_role"
    )
    # Optional restricted runtime account; a single postgres login also works.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_sales_runtime') THEN
        GRANT USAGE ON SCHEMA app TO ai_sales_runtime;
        GRANT SELECT ON app.alembic_version TO ai_sales_runtime;
        GRANT SELECT, INSERT ON app.consent_events TO ai_sales_runtime;
        GRANT SELECT, INSERT, UPDATE ON app.leads, app.lead_contacts,
          app.contact_preferences, app.conversations, app.call_sessions,
          app.transcript_turns TO ai_sales_runtime;
      END IF;
    END $$""")
    op.execute("""CREATE FUNCTION app.set_updated_at() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN NEW.updated_at = clock_timestamp(); RETURN NEW; END; $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION app.set_updated_at() FROM PUBLIC, anon, authenticated, service_role"
    )
    for table in (
        "leads",
        "lead_contacts",
        "contact_preferences",
        "conversations",
        "call_sessions",
    ):
        op.execute(
            f"CREATE TRIGGER set_updated_at BEFORE UPDATE ON app.{table} FOR EACH ROW EXECUTE FUNCTION app.set_updated_at()"
        )


def downgrade():
    # Destructive development rollback: dependent tables first, no CASCADE.
    for table in (
        "transcript_turns",
        "call_sessions",
        "conversations",
        "contact_preferences",
        "consent_events",
        "lead_contacts",
        "leads",
    ):
        op.drop_table(table, schema="app")
    op.execute("DROP FUNCTION app.set_updated_at()")
