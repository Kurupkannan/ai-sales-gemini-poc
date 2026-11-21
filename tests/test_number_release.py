"""A dial the server never got Plivo's answer for must not lock a caller number forever."""
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import SecretStr

from app.auth import calls
from app.config import settings
from app.db.models.identity import OutboundAuthorization
from app.db.session import Database


async def test_unconfirmed_dial_frees_its_number(tmp_path, monkeypatch):
    config = settings.model_copy(update={'DATABASE_URL': SecretStr(f'sqlite+aiosqlite:///{tmp_path}/t.db')})
    db = Database(config)
    monkeypatch.setattr(calls, 'database', db)
    monkeypatch.setattr(settings, 'PLIVO_ORGANIZATION_ID', str(uuid4()))
    monkeypatch.setattr(settings, 'PLIVO_AUTH_ID', 'MA123')
    await db.prepare()
    org = UUID(settings.PLIVO_ORGANIZATION_ID)
    ids = {}
    async with db.transaction() as s:
        for name, request_id in (('lost', None), ('placed', str(uuid4()))):
            row = OutboundAuthorization(id=uuid4(), organization_id=org, actor_id=uuid4(), provider='plivo',
                provider_account_id='MA123', from_number='+918065495916', to_number='+919876543210',
                token_hash=uuid4().hex, idempotency_key=uuid4().hex, state='unknown',
                provider_request_id=request_id, expires_at=calls.now() - timedelta(minutes=5))
            s.add(row)
            ids[name] = row.id
    try:
        await calls.release_unconfirmed(ids['lost'])
        await calls.release_unconfirmed(ids['placed'])
        async with db.transaction() as s:
            lost, placed = await s.get(OutboundAuthorization, ids['lost']), await s.get(OutboundAuthorization, ids['placed'])
            assert (lost.state, lost.end_reason, lost.cleanup_needed) == ('failed', 'dial_unconfirmed', False)
            assert placed.state == 'unknown'  # Plivo has it: normal cleanup applies, not this release.
    finally:
        await db.dispose()
