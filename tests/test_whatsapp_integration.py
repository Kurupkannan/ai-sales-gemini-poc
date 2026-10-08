"""Run against explicitly configured disposable PostgreSQL, with Meta mocked."""
import asyncio
import hashlib
import hmac
import json
import os
from datetime import timedelta
from unittest.mock import AsyncMock
import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, update, func
from app.config import Settings
from app.db.session import Database
from app.api.leads import session_dependency
from app.api import whatsapp as api
from app.whatsapp import security
from app.whatsapp.client import MetaClient, MetaError
from app.db.models.whatsapp import WhatsAppConnection as Connection, WhatsAppWebhookReceipt as Receipt
from app.main import app
from fastapi import Request, HTTPException
from app.auth.dependencies import current_user, Identity
from app.auth import dependencies
from app.db.models.identity import Organization, User, Membership
from contextlib import asynccontextmanager
from uuid import uuid4, UUID
ORG = ''
WABA = ''
PHONE = ''

class TenantDatabase(Database):
    @asynccontextmanager
    async def transaction(self):
        async with super().transaction() as session:
            session.info['organization_id'] = UUID(ORG)
            yield session


pytestmark = pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'), reason='Needs explicitly configured disposable PostgreSQL')


@pytest.fixture
async def context(monkeypatch):
    config = Settings(_env_file=None, DATABASE_URL=os.environ['TEST_DATABASE_URL'], DATABASE_PASSWORD='', DB_SSLMODE='disable',
        WHATSAPP_PROTOTYPE_ENABLED=True, META_APP_ID='123', META_APP_SECRET='test-secret',
        META_EMBEDDED_SIGNUP_CONFIG_ID='456', META_GRAPH_API_VERSION='v25.0', META_WEBHOOK_VERIFY_TOKEN='test-verify',
        WHATSAPP_ENCRYPTION_KEY=Fernet.generate_key().decode(), WHATSAPP_ALLOWED_ORIGINS='https://pilot.test')
    monkeypatch.setattr(api, 'settings', config); monkeypatch.setattr(security, 'settings', config)
    global ORG, WABA, PHONE
    ORG = str(uuid4())
    WABA = str(uuid4().int % 10**16)
    PHONE = str(uuid4().int % 10**16)
    db = TenantDatabase(config)
    async with db.transaction() as s:
        org = Organization(id=UUID(ORG), name='WhatsApp test', slug=ORG, status='active')
        user = User(firebase_uid='wa-'+ORG, email='admin@example.com', display_name='Admin')
        s.add_all([org,user]); await s.flush()
        s.add(Membership(organization_id=org.id, user_id=user.id, role='org_admin'))
        user_id = user.id
    monkeypatch.setattr(dependencies, 'database', db)
    async def identity(request: Request):
        if request.headers.get('authorization') != 'Bearer test': raise HTTPException(401, 'Sign in')
        value = Identity(user_id,'wa-'+ORG,'admin@example.com',False,{'firebase': {'sign_in_second_factor':'totp'}})
        request.state.identity = value
        return value
    app.dependency_overrides[current_user] = identity
    async def session(request: Request):
        db.engine
        async with db._sessions() as value:
            value.info["organization_id"] = getattr(request.state,"organization_id",None)
            value.info["identity"] = getattr(request.state,"identity",None)
            try: yield value
            finally: await value.rollback()
    app.dependency_overrides[session_dependency] = session
    monkeypatch.setattr(MetaClient, 'exchange', AsyncMock(return_value='never-expose-token'))
    monkeypatch.setattr(MetaClient, 'inspect', AsyncMock(return_value={'scopes': ['whatsapp_business_management', 'whatsapp_business_messaging'], 'wabas': [WABA], 'expires_at': None}))
    monkeypatch.setattr(MetaClient, 'phones', AsyncMock(return_value=[{'id': PHONE, 'display_phone_number': '+12025550123', 'verified_name': 'Test Business', 'code_verification_status': 'VERIFIED', 'platform_type': 'CLOUD_API'}]))
    monkeypatch.setattr(MetaClient, 'subscribe', AsyncMock())
    monkeypatch.setattr(MetaClient, 'register', AsyncMock())
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://pilot.test', headers={'Origin': 'https://pilot.test', 'Authorization':'Bearer test'}) as client:
        session_result = await client.post(f'/api/orgs/{ORG}/whatsapp/session')
        assert session_result.status_code == 200, session_result.text
        client.headers['X-WA-CSRF'] = session_result.json()['csrf']
        yield client, db
    app.dependency_overrides.pop(session_dependency, None)
    app.dependency_overrides.pop(current_user, None)
    await db.dispose()


async def start(client):
    result = await client.post(f'/api/orgs/{ORG}/whatsapp/onboarding/start')
    assert result.status_code == 201, result.text
    return result.json()['id']


async def authorize(client):
    ident = await start(client)
    result = await client.post(f'/api/orgs/{ORG}/whatsapp/onboarding/{ident}/complete', json={'code': 'single-use-secret'})
    assert result.status_code == 200, result.text
    assert result.json()['state'] == 'authorized'
    return ident


async def test_full_flow_storage_webhook_and_disconnect(context):
    client, db = context
    ident = await authorize(client)
    verify = await client.post(f'/api/orgs/{ORG}/whatsapp/connections/{ident}/verify')
    assert verify.status_code == 200 and verify.json()['candidate_wabas'] == [WABA]
    numbers = await client.post(f'/api/orgs/{ORG}/whatsapp/connections/{ident}/numbers', json={'waba_id': WABA})
    assert numbers.status_code == 200
    connected = await client.post(f'/api/orgs/{ORG}/whatsapp/connections/{ident}/connect', json={'waba_id': WABA, 'phone_number_id': PHONE})
    assert connected.status_code == 200, connected.text
    assert connected.json()['subscribed'] is True
    assert not connected.json()['registered']
    registered = await client.post(f'/api/orgs/{ORG}/whatsapp/connections/{ident}/register', json={'pin': '123456', 'confirm_new_cloud_number': True})
    assert registered.status_code == 200, registered.text
    assert registered.json()['registered'] is True
    async with db.transaction() as session:
        row = await session.get(Connection, __import__('uuid').UUID(ident))
        assert 'never-expose-token' not in row.token_ciphertext
        assert security.decrypt_token(row.token_ciphertext) == 'never-expose-token'
    body = json.dumps({'object': 'whatsapp_business_account', 'entry': [{'id': WABA, 'changes': []}]}).encode()
    signature = 'sha256=' + hmac.new(b'test-secret', body, hashlib.sha256).hexdigest()
    for _ in range(2):
        response = await client.post('/webhooks/whatsapp', content=body, headers={'X-Hub-Signature-256': signature})
        assert response.status_code == 200, response.text
    async with db.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Receipt).where(Receipt.digest == hashlib.sha256(body).hexdigest())) == 1
    listed = await client.post(f'/api/orgs/{ORG}/whatsapp/connections/list')
    assert 'never-expose-token' not in listed.text and 'ciphertext' not in listed.text
    assert listed.json()[0]['last_webhook_at']
    assert listed.headers['cache-control'] == 'no-store'
    assert (await client.post(f'/api/orgs/{ORG}/whatsapp/connections/{ident}/disconnect')).status_code == 200
    async with db.transaction() as session:
        row = await session.get(Connection, __import__('uuid').UUID(ident))
        assert row.token_ciphertext is None and row.state == 'disconnected'
    assert (await client.post(f'/api/orgs/{ORG}/whatsapp/connections/{ident}/verify')).status_code == 409


async def test_expiry_replay_and_unauthenticated_session(context):
    client, db = context
    ident = await start(client)
    async with db.transaction() as session:
        await session.execute(update(Connection).where(Connection.id == ident).values(expires_at=api.now()-timedelta(seconds=1)))
    assert (await client.post(f'/api/orgs/{ORG}/whatsapp/onboarding/{ident}/complete', json={'code': 'secret'})).status_code == 409
    ident = await authorize(client)
    assert (await client.post(f'/api/orgs/{ORG}/whatsapp/onboarding/{ident}/complete', json={'code': 'secret'})).status_code == 409
    assert MetaClient.exchange.await_count == 1
    # Bearer authentication does not rely on pilot cookies or CSRF headers.
    assert (await client.post(f'/api/orgs/{ORG}/whatsapp/connections/list', headers={'X-WA-CSRF': 'wrong'})).status_code == 200
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://pilot.test') as other:
        assert (await other.post(f'/api/orgs/{ORG}/whatsapp/session')).status_code == 401
        assert (await other.post(f'/api/orgs/{ORG}/whatsapp/connections/{ident}/disconnect')).status_code == 401



async def test_wrong_number_retry_and_concurrent_disconnect(context, monkeypatch):
    client, db = context
    ident = await authorize(client)
    path = f'/api/orgs/{ORG}/whatsapp/connections/{ident}'
    result = await client.post(path+'/connect', json={'waba_id': WABA, 'phone_number_id': '555'})
    assert result.status_code == 502
    MetaClient.subscribe.assert_not_awaited()
    waiting, proceed = asyncio.Event(), asyncio.Event()
    async def subscribe(*args):
        waiting.set(); await proceed.wait()
    monkeypatch.setattr(MetaClient, 'subscribe', subscribe)
    task = asyncio.create_task(client.post(path+'/connect', json={'waba_id': WABA, 'phone_number_id': PHONE}))
    await asyncio.wait_for(waiting.wait(), 5)
    assert (await client.post(path+'/verify')).status_code == 409
    await client.post(path+'/disconnect')
    proceed.set()
    result = await task
    assert result.json()['state'] == 'disconnected'  # late completion cannot revive a disconnected token


async def test_bad_signature_challenge_limits_and_error_redaction(context):
    client, _ = context
    assert (await client.get('/webhooks/whatsapp', params={'hub.mode': 'subscribe', 'hub.verify_token': 'test-verify', 'hub.challenge': '1234'})).text == '1234'
    assert (await client.get('/webhooks/whatsapp', params={'hub.mode': 'subscribe', 'hub.verify_token': 'wrong', 'hub.challenge': '1234'})).status_code == 403
    assert (await client.post('/webhooks/whatsapp', json={})).status_code == 403
    assert (await client.post(f'/api/orgs/{ORG}/whatsapp/onboarding/start', content=b'x'*17000)).status_code == 413
    ident = await start(client)
    bad = await client.post(f'/api/orgs/{ORG}/whatsapp/onboarding/{ident}/complete', json={'code': {'secret': 'do-not-echo'}})
    assert bad.status_code == 422 and 'do-not-echo' not in bad.text


async def test_ambiguous_code_exchange_cannot_retry(context, monkeypatch):
    client, _ = context
    monkeypatch.setattr(MetaClient, 'exchange', AsyncMock(side_effect=MetaError('Meta did not return a usable response. Start signup again.')))
    ident = await start(client)
    path = f'/api/orgs/{ORG}/whatsapp/onboarding/{ident}/complete'
    assert (await client.post(path, json={'code': 'secret'})).status_code == 502
    assert (await client.post(path, json={'code': 'secret'})).status_code == 409
    assert MetaClient.exchange.await_count == 1
