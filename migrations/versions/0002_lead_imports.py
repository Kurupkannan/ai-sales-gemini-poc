"""Lead sheet imports and additional lead details."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("imported_rows", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False),
        sa.Column("invalid_rows", sa.Integer(), nullable=False),
        sa.Column("mapping", postgresql.JSONB(), nullable=False),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("issues", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("fingerprint", name="uq_import_batches_fingerprint"),
        sa.CheckConstraint("total_rows >= 0 AND imported_rows >= 0 AND duplicate_rows >= 0 AND invalid_rows >= 0", name=op.f("ck_import_batches_counts")),
        sa.CheckConstraint("total_rows = imported_rows + duplicate_rows + invalid_rows", name=op.f("ck_import_batches_total")),
        schema="app",
    )
    op.add_column("leads", sa.Column("email", sa.String(254)), schema="app")
    op.add_column("leads", sa.Column("notes", sa.Text()), schema="app")
    op.add_column("leads", sa.Column("extra_fields", postgresql.JSONB(), nullable=False, server_default="{}"), schema="app")
    op.add_column("leads", sa.Column("import_batch_id", sa.Uuid()), schema="app")
    op.create_foreign_key("fk_leads_import_batch_id_import_batches", "leads", "import_batches", ["import_batch_id"], ["id"], source_schema="app", referent_schema="app")
    op.create_index("ix_leads_email", "leads", ["email"], schema="app")
    op.create_index("ix_leads_import_batch_id", "leads", ["import_batch_id"], schema="app")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC, anon, authenticated, service_role")
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_sales_runtime') THEN
        GRANT SELECT, INSERT ON app.import_batches TO ai_sales_runtime;
      END IF;
    END $$""")


def downgrade():
    op.drop_index("ix_leads_import_batch_id", table_name="leads", schema="app")
    op.drop_index("ix_leads_email", table_name="leads", schema="app")
    op.drop_constraint("fk_leads_import_batch_id_import_batches", "leads", schema="app", type_="foreignkey")
    for column in ("import_batch_id", "extra_fields", "notes", "email"):
        op.drop_column("leads", column, schema="app")
    op.drop_table("import_batches", schema="app")
