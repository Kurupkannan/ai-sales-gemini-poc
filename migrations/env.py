"""Use deployment-only credentials and track versions in the private schema."""

import asyncio
from alembic import context
from sqlalchemy import text
from app.config import settings
from app.db.base import Base
from app.db import models  # noqa: F401
from app.db.session import build_engine, connection_url


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == "app"
    return True


def configure(**kwargs):
    context.configure(
        target_metadata=Base.metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="app",
        compare_type=True,
        **kwargs,
    )


def run_migrations(connection):
    # Existing postgres account can bootstrap its own private schema.
    if not connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'app')")):
        connection.execute(text("CREATE SCHEMA app"))
    connection.execute(text("REVOKE ALL ON SCHEMA app FROM PUBLIC"))
    configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = build_engine(
        settings.MIGRATION_DATABASE_URL.get_secret_value(), settings, migration=True
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    configure(
        url=connection_url(
            settings.MIGRATION_DATABASE_URL.get_secret_value(), settings
        ),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.execute("CREATE SCHEMA IF NOT EXISTS app")
        context.execute("REVOKE ALL ON SCHEMA app FROM PUBLIC")
        context.run_migrations()
else:
    asyncio.run(online())
