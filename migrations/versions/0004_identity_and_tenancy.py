"""Firebase identity, organization RBAC, and mandatory business-data isolation."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = depends_on = None
LEGACY = '00000000-0000-4000-8000-000000000001'
TABLES = ['import_batches','leads','lead_contacts','consent_events','contact_preferences',
          'conversations','call_sessions','transcript_turns','whatsapp_connections']
LINKS = [('leads','import_batch_id','import_batches'),('lead_contacts','lead_id','leads'),
    ('consent_events','contact_id','lead_contacts'),('contact_preferences','contact_id','lead_contacts'),
    ('conversations','lead_id','leads'),('conversations','contact_id','lead_contacts'),
    ('call_sessions','conversation_id','conversations'),('transcript_turns','conversation_id','conversations'),
    ('transcript_turns','call_session_id','call_sessions')]

def upgrade():
    for statement in IDENTITY_DDL:
        op.execute(statement)
    op.execute(f"INSERT INTO app.organizations(id,name,slug,status) VALUES ('{LEGACY}', 'Legacy workspace — ownership review required', 'legacy-workspace', 'suspended')")
    for table in TABLES:
        op.add_column(table, sa.Column('organization_id', sa.Uuid()), schema='app')
        op.execute(f"UPDATE app.{table} SET organization_id = '{LEGACY}'")
        op.alter_column(table, 'organization_id', nullable=False, schema='app')
        op.create_foreign_key(f'fk_{table}_organization_id_organizations', table, 'organizations', ['organization_id'], ['id'], source_schema='app', referent_schema='app')
        op.create_unique_constraint(f'uq_{table}_org_id', table, ['organization_id','id'], schema='app')
        op.create_index(f'ix_{table}_organization_id', table, ['organization_id'], schema='app')
        op.execute(f'ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY')
        op.execute(f"CREATE POLICY tenant_isolation ON app.{table} USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid) WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::uuid)")
    op.add_column("whatsapp_connections", sa.Column("initiated_by_uid", sa.String(128)), schema="app")
    for table, column, parent in LINKS:
        op.create_foreign_key(f'fk_{table}_org_{column}', table, parent, ['organization_id',column], ['organization_id','id'], source_schema='app', referent_schema='app')
    op.drop_constraint('uq_import_batches_fingerprint', 'import_batches', schema='app', type_='unique')
    op.create_unique_constraint('uq_import_batches_org_fingerprint', 'import_batches', ['organization_id','fingerprint'], schema='app')
    op.execute('REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC, anon, authenticated, service_role')
    op.execute('''DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_sales_runtime') THEN
        GRANT SELECT, INSERT, UPDATE ON app.users, app.organizations, app.organization_memberships,
          app.organization_invitations, app.email_outbox, app.outbound_authorizations TO ai_sales_runtime;
        GRANT SELECT, INSERT ON app.audit_events TO ai_sales_runtime;
      END IF;
    END $$''')
    # Signed webhook dispatch may touch only routing metadata, never retrieve credentials.
    op.execute("""CREATE FUNCTION app.record_whatsapp_webhook(asset_ids text[]) RETURNS void
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, app AS $$
      DECLARE tenant uuid; previous_context text;
      BEGIN
        previous_context := current_setting('app.organization_id', true);
        FOR tenant IN SELECT id FROM app.organizations WHERE status = 'active' LOOP
          PERFORM set_config('app.organization_id', tenant::text, true);
          UPDATE app.whatsapp_connections SET last_webhook_at = now()
            WHERE organization_id = tenant AND waba_id = ANY(asset_ids) AND state <> 'disconnected';
        END LOOP;
        PERFORM set_config('app.organization_id', coalesce(previous_context, ''), true);
      END; $$""")
    op.execute('REVOKE ALL ON FUNCTION app.record_whatsapp_webhook(text[]) FROM PUBLIC')
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ai_sales_runtime') THEN GRANT EXECUTE ON FUNCTION app.record_whatsapp_webhook(text[]) TO ai_sales_runtime; END IF; END $$")

def downgrade():
    raise RuntimeError('Tenant migration cannot be downgraded safely. Restore a reviewed backup instead.')

# Frozen DDL generated from the initial identity models, independent of future code.
IDENTITY_DDL = [
    "\nCREATE TABLE app.users (\n\tfirebase_uid VARCHAR(128) NOT NULL, \n\temail VARCHAR(254) NOT NULL, \n\tdisplay_name VARCHAR(200) NOT NULL, \n\tactive BOOLEAN DEFAULT 'true' NOT NULL, \n\tplatform_admin BOOLEAN DEFAULT 'false' NOT NULL, \n\tpermissions_version INTEGER DEFAULT '1' NOT NULL, \n\tclaims_retry_at TIMESTAMP WITH TIME ZONE, \n\tclaims_synced_version INTEGER DEFAULT '0' NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_users PRIMARY KEY (id), \n\tCONSTRAINT uq_users_firebase_uid UNIQUE (firebase_uid)\n)\n\n",
    "\nCREATE TABLE app.organizations (\n\tname VARCHAR(120) NOT NULL, \n\tslug VARCHAR(80) NOT NULL, \n\tstatus VARCHAR(20) DEFAULT 'pending' NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_organizations PRIMARY KEY (id), \n\tCONSTRAINT ck_organizations_status CHECK (status IN ('pending','active','suspended','archived')), \n\tCONSTRAINT uq_organizations_slug UNIQUE (slug)\n)\n\n",
    "\nCREATE TABLE app.organization_memberships (\n\torganization_id UUID NOT NULL, \n\tuser_id UUID NOT NULL, \n\trole VARCHAR(20) NOT NULL, \n\tstatus VARCHAR(20) DEFAULT 'active' NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_organization_memberships PRIMARY KEY (id), \n\tCONSTRAINT uq_organization_memberships_organization_id UNIQUE (organization_id, user_id), \n\tCONSTRAINT ck_organization_memberships_role CHECK (role IN ('org_admin','member','viewer')), \n\tCONSTRAINT ck_organization_memberships_status CHECK (status IN ('active','suspended','removed')), \n\tCONSTRAINT fk_organization_memberships_organization_id_organizations FOREIGN KEY(organization_id) REFERENCES app.organizations (id), \n\tCONSTRAINT fk_organization_memberships_user_id_users FOREIGN KEY(user_id) REFERENCES app.users (id)\n)\n\n",
    'CREATE INDEX ix_organization_memberships_user_id ON app.organization_memberships (user_id)',
    'CREATE INDEX ix_organization_memberships_organization_id ON app.organization_memberships (organization_id)',
    "\nCREATE TABLE app.organization_invitations (\n\torganization_id UUID NOT NULL, \n\temail VARCHAR(254) NOT NULL, \n\trole VARCHAR(20) NOT NULL, \n\ttoken_hash VARCHAR(64) NOT NULL, \n\tinvited_by UUID NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\taccepted_by UUID, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_organization_invitations PRIMARY KEY (id), \n\tCONSTRAINT ck_organization_invitations_role CHECK (role IN ('org_admin','member','viewer')), \n\tCONSTRAINT ck_organization_invitations_status CHECK (status IN ('pending','accepted','revoked')), \n\tCONSTRAINT fk_organization_invitations_organization_id_organizations FOREIGN KEY(organization_id) REFERENCES app.organizations (id), \n\tCONSTRAINT uq_organization_invitations_token_hash UNIQUE (token_hash), \n\tCONSTRAINT fk_organization_invitations_invited_by_users FOREIGN KEY(invited_by) REFERENCES app.users (id), \n\tCONSTRAINT fk_organization_invitations_accepted_by_users FOREIGN KEY(accepted_by) REFERENCES app.users (id)\n)\n\n",
    "CREATE UNIQUE INDEX uq_pending_org_email ON app.organization_invitations (organization_id, email) WHERE status = 'pending'",
    'CREATE INDEX ix_organization_invitations_organization_id ON app.organization_invitations (organization_id)',
    '\nCREATE TABLE app.audit_events (\n\torganization_id UUID, \n\tactor_id UUID, \n\taction VARCHAR(100) NOT NULL, \n\ttarget VARCHAR(200) NOT NULL, \n\tdetails JSONB NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_audit_events PRIMARY KEY (id), \n\tCONSTRAINT fk_audit_events_organization_id_organizations FOREIGN KEY(organization_id) REFERENCES app.organizations (id), \n\tCONSTRAINT fk_audit_events_actor_id_users FOREIGN KEY(actor_id) REFERENCES app.users (id)\n)\n\n',
    'CREATE INDEX ix_audit_events_organization_id ON app.audit_events (organization_id)',
    '\nCREATE TABLE app.email_outbox (\n\tinvitation_id UUID NOT NULL, \n\tpayload TEXT, \n\tattempts INTEGER NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tnext_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_email_outbox PRIMARY KEY (id), \n\tCONSTRAINT fk_email_outbox_invitation_id_organization_invitations FOREIGN KEY(invitation_id) REFERENCES app.organization_invitations (id)\n)\n\n',
    '\nCREATE TABLE app.outbound_authorizations (\n\torganization_id UUID NOT NULL, \n\tactor_id UUID NOT NULL, \n\ttoken_hash VARCHAR(64) NOT NULL, \n\tidempotency_key VARCHAR(100) NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tused BOOLEAN NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\tprovider_call_id VARCHAR(200), \n\tid UUID DEFAULT gen_random_uuid() NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_outbound_authorizations PRIMARY KEY (id), \n\tCONSTRAINT uq_outbound_authorizations_organization_id UNIQUE (organization_id, idempotency_key), \n\tCONSTRAINT fk_outbound_authorizations_organization_id_organizations FOREIGN KEY(organization_id) REFERENCES app.organizations (id), \n\tCONSTRAINT fk_outbound_authorizations_actor_id_users FOREIGN KEY(actor_id) REFERENCES app.users (id), \n\tCONSTRAINT uq_outbound_authorizations_token_hash UNIQUE (token_hash)\n)\n\n',
    'CREATE INDEX ix_outbound_authorizations_organization_id ON app.outbound_authorizations (organization_id)'
]
