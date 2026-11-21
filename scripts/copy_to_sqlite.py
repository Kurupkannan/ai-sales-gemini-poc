"""One-off copy of an existing PostgreSQL (Supabase) database into the local SQLite file.

Run from be/:
    .venv/bin/python -m scripts.copy_to_sqlite [--target sqlite+aiosqlite:////abs/path.db]

Reads SUPABASE_MIGRATION_DATABASE_URL (or SUPABASE_DATABASE_URL) from .env, verified
with DB_SSLROOTCERT. Row-level security is forced on tenant tables, so rows are read
once per organization under its context. The target tables must be empty; nothing
is written to PostgreSQL.
"""
import argparse
import asyncio

from dotenv import dotenv_values
from sqlalchemy import select, text, func

from app.config import LOCAL_DATABASE_URL, settings
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.session import build_engine, build_sqlite_engine


async def read_all(source):
    tables = Base.metadata.sorted_tables
    rows = {table.name: {} for table in tables}
    async with source.connect() as connection:
        orgs = [None] + list((await connection.execute(text('SELECT id FROM app.organizations'))).scalars())
        for org in orgs:
            async with connection.begin():
                await connection.execute(text("SELECT set_config('app.organization_id', :org, true)"),
                                         {'org': str(org) if org else ''})
                for table in tables:
                    for row in (await connection.execute(select(table))).mappings():
                        key = tuple(row[c.name] for c in table.primary_key.columns)
                        rows[table.name][key] = dict(row)
    return [(table, list(rows[table.name].values())) for table in tables]


async def main(target_url):
    env = dotenv_values('.env')
    raw = env.get('SUPABASE_MIGRATION_DATABASE_URL') or env.get('SUPABASE_DATABASE_URL') or ''
    if not raw.startswith('postgres'):
        raise SystemExit('Set SUPABASE_MIGRATION_DATABASE_URL in .env to the PostgreSQL source first.')
    source = build_engine(raw, settings, migration=True)
    target = build_sqlite_engine(target_url, settings)
    try:
        data = await read_all(source)
        async with target.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for table, _ in data:
                if await connection.scalar(select(func.count()).select_from(table)):
                    raise SystemExit(f'Target already has rows in {table.name}; refusing to merge.')
            # Insert parents first; users/call_sessions reference each other via nullable FKs.
            await connection.exec_driver_sql('PRAGMA defer_foreign_keys=ON')
            for table, items in data:
                if items:
                    await connection.execute(table.insert(), items)
                print(f'{table.name}: {len(items)}')
    finally:
        await source.dispose()
        await target.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--target', default=LOCAL_DATABASE_URL)
    asyncio.run(main(parser.parse_args().target))
