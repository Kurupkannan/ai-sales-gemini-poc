"""Isolated export tests. No Google requests or real calls."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings, settings
from app.db.models import CallSession, Conversation, Lead, LeadContact
from app.db.session import Database
from app.services import google_sheets as sheets


@pytest.fixture
def export_settings(monkeypatch):
    org = uuid4()
    monkeypatch.setattr(settings, 'GOOGLE_SHEETS_ORGANIZATION_ID', str(org))
    monkeypatch.setattr(settings, 'GOOGLE_SHEETS_WEB_APP_URL', SecretStr('https://script.google.com/macros/s/test/exec'))
    monkeypatch.setattr(settings, 'GOOGLE_SHEETS_ACCESS_CODE', SecretStr('test-export-secret'))
    return org


async def test_scoped_snapshots_latest_call_and_late_summary(tmp_path, monkeypatch, export_settings):
    db = Database(Settings(_env_file=None, DATABASE_URL=f'sqlite+aiosqlite:///{tmp_path}/sheets.db'))
    monkeypatch.setattr(sheets, 'database', db)
    await db.prepare()
    t = datetime(2020, 1, 1, tzinfo=timezone.utc)
    org = export_settings
    try:
        async with db.transaction() as session:
            lead = Lead(organization_id=org, name='Asha', created_at=t, updated_at=t)
            other = Lead(organization_id=uuid4(), name='Private to another org')
            session.add_all([lead, other])
            await session.flush()
            lead_id = lead.id
            contact = LeadContact(organization_id=org, lead_id=lead.id, channel='voice',
                phone_number='+919876543210', created_at=t, updated_at=t)
            session.add(contact)
            await session.flush()
            conversation = Conversation(organization_id=org, lead_id=lead.id,
                contact_id=contact.id, channel='voice')
            session.add(conversation)
            await session.flush()
            conversation_id = conversation.id
            session.add(CallSession(organization_id=org, conversation_id=conversation.id,
                idempotency_key='first', status='completed', created_at=t, updated_at=t,
                result_summary={'lead_status':'WARM', 'summary':'Requested more information.'}))
        rows, _ = await sheets.snapshots(org)
        assert len(rows) == 1 and rows[0]['lead_id'] == str(lead_id)
        assert 'organization_id' not in rows[0]
        assert rows[0]['result'] == 'WARM' and rows[0]['phone'] == '+919876543210'
        async with db.transaction() as session:
            second = CallSession(organization_id=org, conversation_id=conversation_id,
                idempotency_key='second', status='initiated',
                created_at=t+timedelta(minutes=1), updated_at=t+timedelta(minutes=1))
            session.add(second)
            await session.flush()
            call_id = second.id
        rows, _ = await sheets.snapshots(org)
        assert rows[0]['call_id'] == str(call_id)
        assert rows[0]['summary'] is None and rows[0]['result'] == 'UNKNOWN'
        assert rows[0]['send_webinar_link'] is False
        prior_version = rows[0]['updated_at']
        async with db.transaction() as session:
            second = await session.get(CallSession, call_id)
            second.result_summary = {'lead_status':'HOT', 'summary':'Ready now.',
                                     'webinar_response':'YES', 'send_webinar_link':True}
            second.updated_at = t+timedelta(minutes=2)
        rows, _ = await sheets.snapshots(org)
        assert rows[0]['summary'] == 'Ready now.' and rows[0]['send_webinar_link'] is True
        assert rows[0]['updated_at'] > prior_version
        empty, cursor = await sheets.snapshots(org, after=lead_id)
        assert empty == [] and cursor is None
    finally:
        await db.dispose()


async def test_redirect_becomes_get_without_secret_body(export_settings):
    seen = []
    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            assert b'test-export-secret' in request.content
            return httpx.Response(302, headers={'Location':'https://script.googleusercontent.com/reply'})
        assert request.method == 'GET' and request.content == b''
        return httpx.Response(200, json={'ok':True, 'service':'ai-sales-sheets', 'version':3})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await sheets.Exporter(client).request('ping'))['ok']
    assert len(seen) == 2


async def test_failed_write_retries_and_unchanged_rows_do_not_write(monkeypatch, export_settings):
    row = {'lead_id':str(uuid4()), 'updated_at':'2026-10-31T12:00:00Z', 'name':'Asha'}
    async def snapshots(org, after):
        return ([row], row['lead_id']) if after is None else ([], None)
    monkeypatch.setattr(sheets, 'snapshots', snapshots)
    attempts = []
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(200,json={'ok':False,'error':'busy'})
        return httpx.Response(200,json={'ok':True,'result':'updated','row':2})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        exporter = sheets.Exporter(client)
        with pytest.raises(sheets.SheetsError, match='busy'):
            await exporter.sync_once()
        assert exporter.synced == {}
        assert (await exporter.sync_once())['updated'] == 1
        row['updated_at'] = '2026-10-31T12:01:00Z'
        assert (await exporter.sync_once())['unchanged'] == 1
        row['name'] = 'Updated name'
        assert (await exporter.sync_once())['updated'] == 1
    assert len(attempts) == 3


async def test_error_messages_never_echo_provider_data(export_settings):
    def handler(request):
        return httpx.Response(200, json={'ok':False,'error':'test-export-secret customer-data'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(sheets.SheetsError, match='^rejected_snapshot$'):
            await sheets.Exporter(client).request('ping')


def test_live_call_state_wins_over_pending_database_row():
    t = datetime(2026, 10, 31, tzinfo=timezone.utc)
    lead = SimpleNamespace(id=uuid4(),name='Asha',email=None,source=None,
        preferred_language='ml',status='new',created_at=t,updated_at=t)
    live = SimpleNamespace(summary=None,state='active',created_at=t,last_activity=t+timedelta(seconds=3),
        answered_at=t+timedelta(seconds=2),ended_at=None,bill_duration=None,call_session_id=uuid4())
    result = sheets.snapshot(lead, live=live)
    assert result['call_status'] == 'active'
    assert result['call_id'] == str(live.call_session_id)
    assert result['updated_at'] == live.last_activity.isoformat()
