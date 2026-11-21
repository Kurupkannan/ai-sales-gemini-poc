"""Call state/race tests on an explicitly disposable migrated PostgreSQL database."""
import asyncio
import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from app.auth import calls
from app.config import Settings
from app.db.session import Database
from app.db.models import Lead, LeadContact, ContactPreference, CallSession
from app.db.models.identity import Organization, User, Membership, OutboundAuthorization, AuditEvent
from app.telephony import worker
from app.api import leads as lead_api

pytestmark = pytest.mark.skipif(not os.getenv('TEST_AUTH_DATABASE_URL'), reason='Requires disposable PostgreSQL')


@pytest.fixture
async def ctx(plivo_config, monkeypatch):
    db = Database(Settings(_env_file=None, DATABASE_URL=os.environ['TEST_AUTH_DATABASE_URL'], DB_SSLMODE='disable'))
    monkeypatch.setattr(calls, 'database', db)
    org_id, user_id, lead_id, contact_id = [uuid4() for _ in range(4)]
    monkeypatch.setattr(plivo_config, 'PLIVO_ORGANIZATION_ID', str(org_id))
    async with db.transaction() as s:
        s.info['organization_id'] = org_id
        s.add(Organization(id=org_id, name='Plivo test', slug=str(org_id), status='active'))
        s.add(User(id=user_id, firebase_uid=str(user_id), email='test@example.com'))
        await s.flush()
        s.add(Membership(organization_id=org_id, user_id=user_id, role='member', status='active'))
        s.add(Lead(id=lead_id, organization_id=org_id, name='Test'))
        await s.flush()
        s.add(LeadContact(id=contact_id, organization_id=org_id, lead_id=lead_id,
            phone_number='+919876543210', channel='voice'))
        await s.flush()
        s.add(ContactPreference(organization_id=org_id, contact_id=contact_id, suppressed=False, reason='test_consent'))
    payload = SimpleNamespace(contact_id=str(contact_id), to_number='+919876543210', idempotency_key=uuid4().hex)
    actor = SimpleNamespace(id=user_id)
    yield db, org_id, actor, payload
    await db.dispose()


def callback_form(row, **kw):
    return {'CallUUID':str(uuid4()), 'RequestUUID':str(uuid4()), 'Direction':'outbound',
        'From':row.from_number.lstrip('+'), 'To':row.to_number.lstrip('+'), **kw}


async def test_answer_before_create_response_and_one_use_claim(ctx):
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    form = callback_form(row)
    token = await calls.callback(row.id, 'answer', form)
    assert token == await calls.callback(row.id, 'answer', form)
    assert (await calls.dial_result(row.id, request_id=form['RequestUUID']))['status'] == 'active'
    claimed = await asyncio.gather(calls.claim_stream(token), calls.claim_stream(token))
    assert sum(v is not None for v in claimed) == 1
    winner = next(v for v in claimed if v)
    assert winner.provider_call_id == form['CallUUID']
    await calls.start_stream(row.id, 'stream1')
    assert await calls.heartbeat(row.id)
    await calls.callback(row.id, 'hangup', {**form, 'HangupCauseName':'NORMAL_CLEARING'})
    await calls.callback(row.id, 'hangup', {**form, 'HangupCauseName':'NORMAL_CLEARING'})
    assert not await calls.heartbeat(row.id)
    assert (await calls.dial_result(row.id, state='unknown'))['status'] == 'completed'
    state = await calls.read_call(org, row.call_session_id)
    assert state['status'] == 'completed' and state['provider_call_id'] == form['CallUUID']
    history = await calls.calls_for_lead(org, row.lead_id)
    assert len(history) == 1 and history[0]['call_id'] == str(row.call_session_id)
    assert history[0]['status'] == 'completed' and history[0]['answered_at'] is not None
    async with db.transaction() as s:
        s.info['organization_id'] = org
        call = await s.get(CallSession, row.call_session_id)
        assert call.stream_id == 'stream1' and call.ended_at is not None
        lead = await s.get(Lead, (await s.get(LeadContact, UUID(payload.contact_id))).lead_id)
        assert lead.status == 'contacted'
        events = list((await s.scalars(select(AuditEvent).where(AuditEvent.target == str(row.id), AuditEvent.action == 'call.completed'))).all())
        assert len(events) == 1


async def test_reject_cross_org_contact_and_callback_identity(ctx):
    db, org, actor, payload = ctx
    with pytest.raises(HTTPException) as exc:
        await calls.authorize_call(uuid4(), actor, payload)
    assert exc.value.status_code == 503
    row = await calls.authorize_call(org, actor, payload)
    for override in [{'Direction':'inbound'}, {'To':'+919876543212'}]:
        with pytest.raises(HTTPException):
            await calls.callback(row.id, 'answer', callback_form(row, **override))
    form = callback_form(row)
    token = await calls.callback(row.id, 'answer', form)
    with pytest.raises(HTTPException):
        await calls.callback(row.id, 'answer', {**form, 'CallUUID':str(uuid4())})
    with pytest.raises(HTTPException) as exc:
        await calls.read_call(uuid4(), row.call_session_id)
    assert exc.value.status_code == 404
    assert token


async def test_idempotency_concurrency_suppression(ctx):
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    with pytest.raises(HTTPException) as exc:
        await calls.authorize_call(org, actor, payload)
    assert exc.value.status_code == 409
    payload.idempotency_key = uuid4().hex
    with pytest.raises(HTTPException) as exc:
        await calls.authorize_call(org, actor, payload)
    assert exc.value.status_code == 429
    async with db.transaction() as s:
        s.info['organization_id'] = org
        pref = await s.scalar(select(ContactPreference).where(ContactPreference.contact_id == UUID(payload.contact_id)))
        pref.suppressed = True
    with pytest.raises(HTTPException) as exc:
        await calls.authorize_call(org, actor, payload)
    assert exc.value.status_code == 403


async def test_hangup_before_answer_and_expired_or_revoked_grant(ctx):
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    form = callback_form(row)
    await calls.callback(row.id, 'hangup', {**form, 'AnswerTime':'NA', 'CallStatus':'no-answer'})
    assert (await calls.read_call(org, row.call_session_id))['status'] == 'failed'
    assert await calls.callback(row.id, 'answer', form) is None
    assert (await calls.dial_result(row.id, request_id=form['RequestUUID']))['status'] == 'failed'


async def test_expired_and_revoked_stream_token(ctx):
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    token = await calls.callback(row.id, 'answer', callback_form(row))
    async with db.transaction() as s:
        current = await s.get(OutboundAuthorization, row.id)
        current.expires_at = calls.now()-timedelta(seconds=1)
    assert await calls.claim_stream(token) is None
    async with db.transaction() as s:
        current = await s.get(OutboundAuthorization, row.id)
        current.expires_at = calls.now()+timedelta(seconds=300)
        user = await s.get(User, actor.id)
        user.active = False
    assert await calls.claim_stream(token) is None


async def test_cleanup_retry_and_worker_lease(ctx, monkeypatch):
    from app.plivo.client import ProviderError
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    form = callback_form(row)
    await calls.callback(row.id, 'answer', form)
    await calls.request_cleanup(row.id, 'bridge_failure')
    hangup = AsyncMock(side_effect=ProviderError(uncertain=True))
    monkeypatch.setattr(worker.client, 'hangup', hangup)
    await worker.tick()
    assert (await calls.read_call(org, row.call_session_id))['cleanup_pending']
    hangup.side_effect = None
    async with db.transaction() as s:
        current = await s.get(OutboundAuthorization, row.id)
        current.reconcile_after = calls.now()-timedelta(seconds=1)
    await asyncio.gather(worker.tick(), worker.tick())
    assert hangup.await_count == 2  # first failure plus one lease winner
    assert not (await calls.read_call(org, row.call_session_id))['cleanup_pending']


async def test_disconnect_then_hangup_corrects_provisional_failure(ctx):
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    form = callback_form(row)
    await calls.callback(row.id, 'answer', form)
    await calls.request_cleanup(row.id, 'media_disconnected')
    await calls.cleanup_complete(row.id)
    await calls.callback(row.id, 'hangup', {**form, 'HangupCauseName':'NORMAL_CLEARING'})
    assert (await calls.read_call(org, row.call_session_id))['status'] == 'completed'


async def test_manual_lead_requires_recorded_permission(ctx, monkeypatch):
    db, org, actor, _ = ctx
    monkeypatch.setattr(lead_api, 'database', db)
    request = SimpleNamespace(state=SimpleNamespace(organization_id=org, identity=actor))
    payload = lead_api.NewLead(name='Manual lead', phone='+919876543212')
    created = await lead_api.create_lead(payload, request)
    contact_id = UUID(created['contact_id'])
    async with db.transaction() as s:
        s.info['organization_id'] = org
        preference = await s.scalar(select(ContactPreference).where(ContactPreference.contact_id == contact_id))
        assert preference.suppressed
    await lead_api.set_calling_permission(UUID(created['id']), contact_id,
        lead_api.CallingPermission(allowed=True, evidence='Customer requested a test call by phone.'), request)
    async with db.transaction() as s:
        s.info['organization_id'] = org
        preference = await s.scalar(select(ContactPreference).where(ContactPreference.contact_id == contact_id))
        assert not preference.suppressed
    with pytest.raises(HTTPException) as exc:
        await lead_api.create_lead(payload, request)
    assert exc.value.status_code == 409


async def test_concurrent_authorization_reserves_one_slot(ctx):
    db, org, actor, payload = ctx
    second = SimpleNamespace(**{**vars(payload), 'idempotency_key':uuid4().hex})
    results = await asyncio.gather(calls.authorize_call(org, actor, payload),
        calls.authorize_call(org, actor, second), return_exceptions=True)
    assert sum(isinstance(r, OutboundAuthorization) for r in results) == 1
    assert next(r for r in results if isinstance(r, HTTPException)).status_code == 429


async def test_reconcile_missing_hangup_and_cdr_failure_timeout(ctx, monkeypatch):
    from app.plivo.client import ProviderError
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    form = callback_form(row)
    token = await calls.callback(row.id, 'answer', form)
    await calls.claim_stream(token)
    async with db.transaction() as s:
        current = await s.get(OutboundAuthorization, row.id)
        current.heartbeat_at = calls.now() - timedelta(seconds=100)
        current.reconcile_after = calls.now() - timedelta(seconds=1)
    monkeypatch.setattr(worker.client, 'get_call', AsyncMock(side_effect=ProviderError(uncertain=True)))
    hangup = AsyncMock()
    monkeypatch.setattr(worker.client, 'hangup', hangup)
    await worker.tick()
    hangup.assert_awaited_once_with(form['CallUUID'])
    result = await calls.read_call(org, row.call_session_id)
    assert result['status'] == 'failed' and result['end_reason'] == 'session_timeout'


async def test_tenant_session_reference_enforced_by_database(ctx):
    from sqlalchemy.exc import IntegrityError
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    other = uuid4()
    async with db.transaction() as s:
        s.add(Organization(id=other, name='Other', slug=str(other), status='active'))
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            s.add(OutboundAuthorization(organization_id=other, actor_id=actor.id,
                call_session_id=row.call_session_id, token_hash=calls.digest(uuid4().hex),
                idempotency_key=uuid4().hex, expires_at=calls.now()+timedelta(minutes=5)))


async def test_operator_resolution_only_after_unknown_unidentified_attempt(ctx):
    from scripts.resolve_call import resolve
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    with pytest.raises(ValueError):
        await resolve(row.id, 'Verified no provider record exists')
    await calls.dial_result(row.id, state='unknown')
    await resolve(row.id, 'Verified no provider record exists; test fixture')
    state = await calls.read_call(org, row.call_session_id)
    assert state['status'] == 'failed' and not state['cleanup_pending']
    with pytest.raises(HTTPException) as exc:
        await calls.authorize_call(org, actor, payload)
    assert exc.value.status_code == 409


async def test_drained_historical_attempt_does_not_take_plivo_slot(ctx):
    db, org, actor, payload = ctx
    async with db.transaction() as s:
        s.add(OutboundAuthorization(organization_id=org, actor_id=actor.id,
            provider='legacy', state='initiated', token_hash=calls.digest(uuid4().hex),
            idempotency_key=uuid4().hex, expires_at=calls.now()-timedelta(days=1),
            created_at=calls.now()-timedelta(days=1)))
    assert (await calls.authorize_call(org, actor, payload)).provider == 'plivo'


async def test_list_count_reuse_preserves_status_search_and_empty_pages(ctx):
    db, org, actor, payload = ctx
    async with db.transaction() as session:
        session.info['organization_id'] = org
        session.add(Lead(organization_id=org, name='Qualified prospect', status='qualified'))
    async with db.transaction() as session:
        session.info['organization_id'] = org
        all_rows = await lead_api.leads(session, q='', status=None, page=1, page_size=20)
        qualified = await lead_api.leads(session, q='', status='qualified', page=1, page_size=20)
        searched = await lead_api.leads(session, q='prospect', status=None, page=1, page_size=20)
        empty_page = await lead_api.leads(session, q='', status=None, page=10, page_size=20)
    assert all_rows['total'] == 2
    assert qualified['total'] == searched['total'] == 1
    assert qualified['items'][0]['name'] == searched['items'][0]['name']
    assert empty_page['total'] == 2 and empty_page['items'] == []
    assert all_rows['stats'] == qualified['stats'] == {'total': 2, 'new': 1, 'qualified': 1}


async def test_existing_call_results_populate_lead_type_and_filter(ctx):
    db, org, actor, payload = ctx
    row = await calls.authorize_call(org, actor, payload)
    async with db.transaction() as s:
        s.info['organization_id'] = org
        call = await s.get(CallSession, row.call_session_id)
        call.result_summary = {'lead_status': 'CALLBACK', 'preferred_callback_time': 'tomorrow', 'summary': 'Busy; call tomorrow.'}
    async with db.transaction() as s:
        s.info['organization_id'] = org
        result = await lead_api.leads(s, q='', status=None, page=1, page_size=20, lead_type='CALLBACK')
        assert result['total'] == 1 and result['items'][0]['lead_type'] == 'CALLBACK'
        assert (await lead_api.lead_detail(row.lead_id, s))['lead_type'] == 'CALLBACK'
        assert (await lead_api.leads(s, q='', status=None, page=1, page_size=20, lead_type='HOT'))['total'] == 0
        # Newer failed/unclassified attempts must not erase the last useful outcome.
        s.add(CallSession(organization_id=org, conversation_id=row.conversation_id,
            idempotency_key=uuid4().hex, status='failed', result_summary={'lead_status': 'UNKNOWN'},
            created_at=calls.now()+timedelta(seconds=1)))
    async with db.transaction() as s:
        s.info['organization_id'] = org
        result = await lead_api.leads(s, q='', status=None, page=1, page_size=20)
        assert result['items'][0]['lead_type'] == 'CALLBACK'
        s.add(CallSession(organization_id=org, conversation_id=row.conversation_id,
            idempotency_key=uuid4().hex, status='completed', result_summary={'lead_status': 'HOT'},
            created_at=calls.now()+timedelta(seconds=2)))
    async with db.transaction() as s:
        s.info['organization_id'] = org
        result = await lead_api.leads(s, q='', status=None, page=1, page_size=20, lead_type='HOT')
        assert result['total'] == 1 and result['items'][0]['lead_type'] == 'HOT'
    async with db.transaction() as s:
        s.info['organization_id'] = uuid4()
        assert (await lead_api.leads(s, q='', status=None, page=1, page_size=20))['items'] == []
