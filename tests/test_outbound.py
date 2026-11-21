"""Plivo call construction, failure certainty and scoped API contracts."""
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.auth.dependencies import organization_access
from app.api import calls as api
from app.plivo import client
from app.telephony import service

ORG = '10000000-0000-4000-8000-000000000001'
REQUEST_ID = '10000000-0000-4000-8000-000000000002'


def attempt():
    return SimpleNamespace(id=uuid4(), from_number='+919876543211', to_number='+919876543210')


async def test_plivo_request(plivo_config, monkeypatch):
    send = AsyncMock(return_value=httpx.Response(201, json={'request_uuid': REQUEST_ID}))
    monkeypatch.setattr(client, 'request', send)
    row = attempt()
    assert await client.create_call(row) == REQUEST_ID
    args, kw = send.call_args
    assert args == ('POST', 'Call/')
    body = kw['json']
    assert body['from'] == row.from_number and body['to'] == row.to_number
    assert body['answer_url'] == f'https://voice.example.com/plivo/answer/{row.id}'
    assert body['hangup_url'].endswith(f'/hangup/{row.id}')
    assert body['ring_url'].endswith(f'/ring/{row.id}')
    assert body['answer_method'] == body['ring_method'] == body['hangup_method'] == 'POST'
    assert body['time_limit'] == 600
    assert body['ring_timeout'] == 45
    assert 'hangup_on_ring' not in body


async def test_http_auth_and_no_redirects(plivo_config, monkeypatch):
    send = AsyncMock(return_value=httpx.Response(201))
    instance = SimpleNamespace(request=send, is_closed=False, aclose=AsyncMock())
    from app import http_clients
    monkeypatch.setattr(http_clients, '_clients', {})
    captured = {}
    def create(**kw):
        captured.update(kw)
        return instance
    monkeypatch.setattr(client.httpx, 'AsyncClient', create)
    await client.request('POST', 'Call/', json={'to': 'test'})
    assert captured['follow_redirects'] is False
    assert send.call_args.args == ('POST', 'https://api.plivo.com/v1/Account/MA123456789/Call/')
    assert send.call_args.kwargs['auth'] == ('MA123456789', 'unit-test-token')
    await client.request('GET', 'Call/')
    assert send.await_count == 2
    assert len(http_clients._clients) == 1
    await http_clients.close_clients()
    instance.aclose.assert_awaited_once()


@pytest.mark.parametrize('status,body,uncertain', [
    (400, {}, False), (401, {}, False), (429, {}, False), (500, {}, True),
    (408, {}, True), (201, {}, True), (201, {'request_uuid': 'bad'}, True),
])
async def test_dial_failures_do_not_retry(plivo_config, monkeypatch, status, body, uncertain):
    send = AsyncMock(return_value=httpx.Response(status, json=body))
    monkeypatch.setattr(client, 'request', send)
    with pytest.raises(client.ProviderError) as exc:
        await client.create_call(attempt())
    assert exc.value.uncertain is uncertain
    send.assert_awaited_once()


async def test_uncertain_result_stays_in_memory_until_call_ends(plivo_config, monkeypatch):
    row = attempt()
    authorize = AsyncMock(return_value=row)
    live = SimpleNamespace(id=row.id, state='pending', snapshot=lambda: {'status':'unknown'})
    monkeypatch.setattr(service, 'begin', lambda _: None)
    monkeypatch.setattr(service, 'wait_for_greeting', AsyncMock())
    monkeypatch.setattr(service.calls, 'authorize_call', authorize)
    monkeypatch.setattr(service.runtime, 'register', lambda _: live)
    monkeypatch.setattr(client, 'create_call', AsyncMock(side_effect=client.ProviderError(uncertain=True)))
    assert await service.initiate(ORG, object(), object()) == {'status': 'unknown'}
    assert live.state == 'unknown'


@pytest.fixture
def authorized():
    async def identity(request: Request):
        request.state.organization_id = UUID(ORG)
        request.state.permissions = {'calls.create', 'calls.read'}
        return object()
    app.dependency_overrides[organization_access] = identity
    yield
    app.dependency_overrides.pop(organization_access, None)


async def test_create_api_no_media_secrets(plivo_config, monkeypatch, authorized):
    result = {'call_id': str(uuid4()), 'attempt_id': str(uuid4()), 'provider': 'plivo',
        'status': 'initiated', 'provider_request_id': REQUEST_ID}
    initiate = AsyncMock(return_value=result)
    monkeypatch.setattr(api, 'initiate', initiate)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post(f'/api/orgs/{ORG}/calls', json={
            'contact_id': ORG, 'to_number': '+919876543210', 'idempotency_key': 'call-request-123456'})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['provider_request_id'] == REQUEST_ID
    assert data['provider_call_id'] is None
    assert not {'stream_url', 'call_sid', 'token', 'details'} & data.keys()


async def test_disabled_before_provider_call(plivo_config, monkeypatch, authorized):
    monkeypatch.setattr(plivo_config, 'OUTBOUND_CALLS_ENABLED', False)
    send = AsyncMock()
    monkeypatch.setattr(api, 'initiate', send)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post(f'/api/orgs/{ORG}/calls', json={
            'contact_id': ORG, 'to_number': '+919876543210', 'idempotency_key': 'call-request-123456'})
    assert response.status_code == 503
    send.assert_not_called()


async def test_stop_call_is_tenant_scoped(plivo_config, monkeypatch, authorized):
    call_id, attempt_id = uuid4(), uuid4()
    read = AsyncMock(return_value={'call_id': str(call_id), 'attempt_id': str(attempt_id),
        'provider': 'plivo', 'status': 'active', 'cleanup_pending': False})
    stop = AsyncMock()
    monkeypatch.setattr(api, 'read_call', read)
    monkeypatch.setattr(api, 'terminate', stop)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post(f'/api/orgs/{ORG}/calls/{call_id}/stop')
    assert response.status_code == 200
    read.assert_awaited_with(UUID(ORG), call_id)
    stop.assert_awaited_once_with(attempt_id, 'stopped_by_operator')
