"""One-shot operator setup: restricted roles, then the caller runs Alembic."""
import asyncio
import secrets
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine.url import make_url

from app.config import settings
from app.db.session import Database

RUNTIME = "ai_sales_runtime"
MIGRATOR = "ai_sales_migrator"
HISTORICAL_GRANTS = [
    "GRANT CONNECT ON DATABASE postgres TO ai_sales_runtime, ai_sales_migrator",
    "GRANT USAGE ON SCHEMA app TO ai_sales_runtime",
    "GRANT SELECT ON app.alembic_version TO ai_sales_runtime",
    "GRANT SELECT, INSERT ON app.consent_events TO ai_sales_runtime",
    "GRANT SELECT, INSERT, UPDATE ON app.leads, app.lead_contacts, app.contact_preferences, app.conversations, app.call_sessions, app.transcript_turns TO ai_sales_runtime",
    "GRANT SELECT, INSERT ON app.import_batches TO ai_sales_runtime",
    "GRANT SELECT, INSERT, UPDATE ON app.whatsapp_connections TO ai_sales_runtime",
    "GRANT SELECT, INSERT ON app.whatsapp_webhook_receipts TO ai_sales_runtime",
    "GRANT ai_sales_migrator TO postgres",
]


async def ensure_role(conn, name, password):
    exists = await conn.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}
    )
    verb = "ALTER ROLE" if exists else "CREATE ROLE"
    template = (
        f"{verb} {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOREPLICATION NOBYPASSRLS PASSWORD %L"
    )
    sql = await conn.scalar(
        text("SELECT format(CAST(:template AS text), CAST(:password AS text))"),
        {"template": template, "password": password},
    )
    await conn.execute(text(sql))
    return "updated" if exists else "created"


def rewrite_env(runtime_password, migrator_password):
    env_path = Path(__file__).resolve().parents[1] / ".env"
    pg_password = settings.DATABASE_PASSWORD.get_secret_value()
    lines = []
    saw_migrator = False
    for line in env_path.read_text().splitlines():
        if line.startswith("DATABASE_URL="):
            url = make_url(line.split("=", 1)[1])
            project_ref = url.username.split(".", 1)[1]
            url = url.set(username=f"{RUNTIME}.{project_ref}", password=None)
            lines.append("DATABASE_URL=" + url.render_as_string(hide_password=False))
        elif line.startswith("MIGRATION_DATABASE_URL="):
            url = make_url(line.split("=", 1)[1]).set(password=pg_password)
            lines.append(
                "MIGRATION_DATABASE_URL=" + url.render_as_string(hide_password=False)
            )
        elif line.startswith("DATABASE_PASSWORD="):
            lines.append("DATABASE_PASSWORD=" + runtime_password)
        elif line.startswith("MIGRATOR_DATABASE_PASSWORD="):
            saw_migrator = True
            lines.append("MIGRATOR_DATABASE_PASSWORD=" + migrator_password)
        else:
            lines.append(line)
    if not saw_migrator:
        lines.append("MIGRATOR_DATABASE_PASSWORD=" + migrator_password)
    env_path.write_text("\n".join(lines) + "\n")


async def main():
    runtime_password = secrets.token_urlsafe(24)
    migrator_password = secrets.token_urlsafe(24)
    db = Database(settings)
    async with db.engine.begin() as conn:
        print(f"role {RUNTIME}: {await ensure_role(conn, RUNTIME, runtime_password)}")
        print(f"role {MIGRATOR}: {await ensure_role(conn, MIGRATOR, migrator_password)}")
        for statement in HISTORICAL_GRANTS:
            await conn.execute(text(statement))
        await conn.execute(text("GRANT CREATE ON SCHEMA public TO postgres"))
        await conn.execute(text("GRANT CREATE ON DATABASE postgres TO postgres"))
        await conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        await conn.execute(text("REVOKE CREATE ON DATABASE postgres FROM PUBLIC"))
        flags = (
            await conn.execute(
                text(
                    """
                    SELECT
                      has_schema_privilege('ai_sales_runtime', 'public', 'CREATE'),
                      has_schema_privilege('ai_sales_runtime', 'app', 'CREATE'),
                      has_database_privilege('ai_sales_runtime', current_database(), 'CREATE'),
                      has_schema_privilege('postgres', 'public', 'CREATE')
                    """
                )
            )
        ).one()
        print("runtime_public_create", flags[0])
        print("runtime_app_create", flags[1])
        print("runtime_db_create", flags[2])
        print("postgres_public_create", flags[3])
    await db.dispose()
    # Persist passwords only after the roles commit, before Alembic.
    secret_path = Path(__file__).resolve().parents[1] / ".auth-role-secrets"
    secret_path.write_text(
        f"RUNTIME={runtime_password}\nMIGRATOR={migrator_password}\n"
    )
    secret_path.chmod(0o600)
    print("secrets_staged")


if __name__ == "__main__":
    asyncio.run(main())
