"""Read-only connectivity, schema version and role-permission verification."""

import asyncio
import sys
from alembic.config import Config
from alembic.script import ScriptDirectory
from pathlib import Path
from sqlalchemy import text
from app.config import settings
from app.db.session import Database, DatabaseConfigurationError

TABLES = (
    "import_batches",
    "leads",
    "lead_contacts",
    "consent_events",
    "contact_preferences",
    "conversations",
    "call_sessions",
    "transcript_turns",
)


class DatabaseCheckError(RuntimeError):
    """Actionable diagnostic containing no credentials or provider errors."""


async def check_database(db: Database) -> None:
    root = Path(__file__).resolve().parents[1]
    head = ScriptDirectory.from_config(
        Config(str(root / "alembic.ini"))
    ).get_current_head()
    async with db.engine.connect() as conn:
        if await conn.scalar(text("SELECT 1")) != 1:
            raise DatabaseCheckError("Database probe returned an unexpected result.")
        role = (
            await conn.execute(
                text(
                    "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        if role[0] == "postgres":
            versions = (await conn.execute(text("SELECT version_num FROM app.alembic_version"))).scalars().all()
            if versions != [head]:
                raise DatabaseCheckError("Run alembic upgrade head to update the schema.")
            for table in TABLES:
                await conn.execute(text(f"SELECT 1 FROM app.{table} LIMIT 0"))
            print("Using the configured postgres account (administrative database access).")
            return
        if role[0] != "ai_sales_runtime" or any(role[1:]):
            raise DatabaseCheckError(
                "DATABASE_URL must use the restricted ai_sales_runtime role."
            )
        if await conn.scalar(
            text("SELECT pg_has_role(current_user, 'ai_sales_migrator', 'MEMBER')")
        ):
            raise DatabaseCheckError("Runtime must not inherit migration privileges.")
        if await conn.scalar(
            text(
                "SELECT has_schema_privilege(current_user, 'app', 'CREATE') OR has_schema_privilege(current_user, 'public', 'CREATE') OR has_database_privilege(current_user, current_database(), 'CREATE')"
            )
        ):
            raise DatabaseCheckError("Runtime role has schema-management privileges.")
        # Runtime may read the version table, but must not write it.
        versions = (
            (await conn.execute(text("SELECT version_num FROM app.alembic_version")))
            .scalars()
            .all()
        )
        if versions != [head]:
            raise DatabaseCheckError(
                "Database migration version differs from the application; run alembic upgrade head."
            )
        for table in TABLES:
            name = f"app.{table}"
            for privilege in ("SELECT", "INSERT"):
                if not await conn.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, :table, :privilege)"
                    ),
                    {"table": name, "privilege": privilege},
                ):
                    raise DatabaseCheckError(
                        f"Runtime is missing {privilege} on {name}."
                    )
            expected_update = table not in ("consent_events", "import_batches")
            if (
                await conn.scalar(
                    text("SELECT has_table_privilege(current_user, :table, 'UPDATE')"),
                    {"table": name},
                )
                != expected_update
            ):
                raise DatabaseCheckError(f"Unexpected UPDATE permission on {name}.")
            if await conn.scalar(
                text(
                    "SELECT has_table_privilege(current_user, :table, 'DELETE,TRUNCATE,TRIGGER,REFERENCES')"
                ),
                {"table": name},
            ):
                raise DatabaseCheckError(
                    f"Unexpected elevated table permissions on {name}."
                )
            for browser_role in ("anon", "authenticated", "service_role"):
                if await conn.scalar(
                    text(
                        "SELECT has_schema_privilege(:role, 'app', 'USAGE') OR has_table_privilege(:role, :table, 'SELECT,INSERT,UPDATE,DELETE')"
                    ),
                    {"role": browser_role, "table": name},
                ):
                    raise DatabaseCheckError(
                        f"Unexpected Data API role access to {name}."
                    )
        if await conn.scalar(
            text(
                "SELECT has_table_privilege(current_user, 'app.alembic_version', 'INSERT,UPDATE,DELETE,TRUNCATE')"
            )
        ):
            raise DatabaseCheckError("Runtime must not write migration history.")


async def main() -> int:
    db = Database(settings)
    try:
        await check_database(db)
    except (DatabaseCheckError, DatabaseConfigurationError) as exc:
        print(f"Database check failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Driver exceptions may contain connection strings or statement parameters.
        print(
            f"Database check failed ({type(exc).__name__}). Check URLs, TLS, bootstrap roles and migrations in docs/SUPABASE_SETUP.md.",
            file=sys.stderr,
        )
        return 1
    finally:
        await db.dispose()
    print("Database connection and schema checks: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
