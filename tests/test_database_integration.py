"""Run only against a bootstrapped disposable PostgreSQL database.

Set TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL. This test downgrades
and recreates the app schema tables: never point these at shared/live data.
"""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from app.config import Settings
from app.db.models import Lead, LeadContact, Conversation, CallSession, TranscriptTurn
from app.db.session import Database
from app.db.models.identity import Organization
from scripts.check_database import check_database

pytestmark = pytest.mark.skipif(
    not (os.getenv("TEST_DATABASE_URL") and os.getenv("TEST_MIGRATION_DATABASE_URL")),
    reason="Requires explicitly configured disposable PostgreSQL database",
)


async def test_migrations_permissions_constraints_and_rollback():
    root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "MIGRATION_DATABASE_URL": os.environ["TEST_MIGRATION_DATABASE_URL"],
        "DB_SSLMODE": "disable",
        "DB_SSLROOTCERT": "",
    }

    def migrate(*args):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
        )

    migrate("upgrade", "head")
    db = Database(
        Settings(_env_file=None, DATABASE_URL=env["DATABASE_URL"], DB_SSLMODE="disable")
    )
    try:
        await check_database(db)
        org_id = uuid4()
        async with db.transaction() as session:
            session.add(Organization(id=org_id, name='Database test', slug=str(org_id), status='active'))
        # Ensure migration metadata and current models agree.
        migrate("check")
        lead_id, contact_id, conversation_id, call_id = [uuid4() for _ in range(4)]
        other_lead_id = uuid4()
        async with db.transaction() as session:
            session.info["organization_id"] = org_id
            session.add(Lead(organization_id=org_id, id=other_lead_id, name="Other lead"))
            session.add(Lead(organization_id=org_id, id=lead_id, name="Database test"))
            await session.flush()
            session.add(
                LeadContact(organization_id=org_id,
                    id=contact_id,
                    lead_id=lead_id,
                    phone_number="+919876543210",
                    channel="voice",
                )
            )
            await session.flush()
            session.add(
                Conversation(organization_id=org_id,
                    id=conversation_id,
                    lead_id=lead_id,
                    contact_id=contact_id,
                    channel="voice",
                )
            )
            await session.flush()
            session.add(
                CallSession(organization_id=org_id,
                    id=call_id,
                    conversation_id=conversation_id,
                    idempotency_key=str(uuid4()),
                    provider_call_id="test-" + str(call_id),
                )
            )
            await session.flush()
            session.add(
                TranscriptTurn(organization_id=org_id,
                    conversation_id=conversation_id,
                    call_session_id=call_id,
                    sequence=0,
                    speaker="customer",
                    text="Test",
                    occurred_at=datetime.now(timezone.utc),
                )
            )
        async with db.transaction() as session:
            session.info["organization_id"] = org_id
            lead = await session.get(Lead, lead_id)
            assert lead.created_at.tzinfo is not None
            previous = lead.updated_at
            lead.name = "Updated"
        async with db.transaction() as session:
            session.info["organization_id"] = org_id
            assert (await session.get(Lead, lead_id)).updated_at > previous
        # Unique provider IDs, transcript sequence, E.164 and composite FKs.
        invalid_records = [
            CallSession(organization_id=org_id,
                conversation_id=conversation_id,
                idempotency_key=str(uuid4()),
                provider_call_id="test-" + str(call_id),
            ),
            TranscriptTurn(organization_id=org_id,
                conversation_id=conversation_id,
                call_session_id=call_id,
                sequence=0,
                speaker="customer",
                text="Duplicate",
                occurred_at=datetime.now(timezone.utc),
            ),
            LeadContact(organization_id=org_id, lead_id=lead_id, phone_number="invalid", channel="voice"),
            Conversation(organization_id=org_id, lead_id=other_lead_id, contact_id=contact_id, channel="voice"),
        ]
        for record in invalid_records:
            with pytest.raises(IntegrityError):
                async with db.transaction() as session:
                    session.info["organization_id"] = org_id
                    session.add(record)
        # A failed unit of work must rollback the insert and return the connection.
        rolled_back_id = uuid4()
        with pytest.raises(RuntimeError):
            async with db.transaction() as session:
                session.info["organization_id"] = org_id
                session.add(Lead(organization_id=org_id, id=rolled_back_id))
                await session.flush()
                raise RuntimeError("abort")
        async with db.transaction() as session:
            session.info["organization_id"] = org_id
            assert await session.get(Lead, rolled_back_id) is None
        for statement in (
            "CREATE TABLE app.forbidden(id int)",
            "ALTER TABLE app.leads ADD COLUMN forbidden int",
            "UPDATE app.consent_events SET action='revoked'",
            "DELETE FROM app.leads",
        ):
            with pytest.raises(DBAPIError):
                async with db.transaction() as session:
                    session.info["organization_id"] = org_id
                    await session.execute(text(statement))
        # Cancellation must also rollback and release the connection.
        import asyncio

        with pytest.raises(asyncio.CancelledError):
            async with db.transaction() as session:
                session.info["organization_id"] = org_id
                session.add(Lead(organization_id=org_id, id=rolled_back_id))
                await session.flush()
                raise asyncio.CancelledError()
        async with db.transaction() as session:
            session.info["organization_id"] = org_id
            assert await session.get(Lead, rolled_back_id) is None
    finally:
        await db.dispose()
    with pytest.raises(subprocess.CalledProcessError):
        migrate("downgrade", "base")  # Tenant removal intentionally requires a reviewed restore.
    migrate("upgrade", "head")
    try:
        await check_database(db)
    finally:
        await db.dispose()
