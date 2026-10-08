"""Meta client and security tests with no real accounts or network calls."""
import hashlib
import hmac
import json
import logging
from unittest.mock import AsyncMock
import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from app.config import Settings
from app.whatsapp import security
from app.whatsapp.client import MetaClient, MetaError


@pytest.fixture
def configured(monkeypatch):
    config = Settings(_env_file=None, WHATSAPP_PROTOTYPE_ENABLED=True,
        DATABASE_URL='postgresql://postgres@localhost/test', META_APP_ID='123', META_APP_SECRET='app-secret',
        META_EMBEDDED_SIGNUP_CONFIG_ID='456', META_GRAPH_API_VERSION='v25.0',
        META_WEBHOOK_VERIFY_TOKEN='verify-secret', WHATSAPP_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        WHATSAPP_ALLOWED_ORIGINS='https://pilot.example.test')
    monkeypatch.setattr(security, 'settings', config)
    monkeypatch.setattr('app.whatsapp.client.settings', config)
    return config


def req(headers):
    return Request({'type': 'http', 'headers': [(k.encode(), v.encode()) for k, v in headers.items()]})


def test_configuration_and_secret_storage(configured):
    assert security.missing_settings() == []
    encrypted = security.encrypt_token('private-token')
    assert 'private-token' not in encrypted
    assert security.decrypt_token(encrypted) == 'private-token'
    with pytest.raises(HTTPException): security.decrypt_token(encrypted[:-3])
    assert 'app-secret' not in repr(configured)
    configured.WHATSAPP_ALLOWED_ORIGINS = 'http://remote.example.test'
    assert security.missing_settings()


def test_session_csrf_origin_and_domain_separation(configured):
    response = Response()
    token = security.new_session(req({'origin': 'https://pilot.example.test'}), response)
    cookie = response.headers['set-cookie'].split(';', 1)[0]
    headers = {'origin': 'https://pilot.example.test', 'cookie': cookie, 'x-wa-csrf': token}
    assert security.authorized(req(headers)) == hashlib.sha256(token.encode()).hexdigest()
    assert 'HttpOnly' in response.headers['set-cookie'] and 'Secure' in response.headers['set-cookie']
    for changed in ({'origin': 'https://pilot.example.test.evil.com'}, {'x-wa-csrf': 'wrong'}, {'cookie': ''}):
        with pytest.raises(HTTPException): security.authorized(req(headers | changed))
    # A database token ciphertext must never be accepted as a browser session.
    with pytest.raises(HTTPException):
        security.read_session(req({'cookie': 'wa_pilot_session=' + security.encrypt_token('x'*64)}))


@pytest.mark.parametrize('data', [
    {'is_valid': False},
    {'is_valid': True, 'app_id': 'other'},
    {'is_valid': True, 'app_id': '123', 'scopes': ['whatsapp_business_management']},
    {'is_valid': True, 'app_id': '123', 'scopes': ['whatsapp_business_management', 'whatsapp_business_messaging'], 'expires_at': 1},
])
async def test_reject_invalid_token(configured, data):
    client = MetaClient(); client.call = AsyncMock(return_value={'data': data})
    with pytest.raises(MetaError): await client.inspect('token')


async def test_token_and_paginated_phone_verification(configured):
    client = MetaClient()
    client.call = AsyncMock(return_value={'data': {'is_valid': True, 'app_id': '123', 'scopes': ['whatsapp_business_management', 'whatsapp_business_messaging'], 'granular_scopes': [{'scope': 'whatsapp_business_management', 'target_ids': ['789']}], 'expires_at': 0}})
    assert (await client.inspect('token'))['wabas'] == ['789']
    client.call = AsyncMock(side_effect=[{'id': '789'}, {'data': [{'id': '111'}], 'paging': {'next': 'https://evil.test/steal', 'cursors': {'after': 'cursor'}}}, {'data': [{'id': '222'}]}])
    assert [p['id'] for p in await client.phones('token', '789')] == ['111', '222']
    assert client.call.call_args.args[1] == '789/phone_numbers'
    assert client.call.call_args.kwargs['params']['after'] == 'cursor'


async def test_subscription_requires_confirmation(configured):
    client = MetaClient(); client.call = AsyncMock(side_effect=[{'success': True}, {'data': []}])
    with pytest.raises(MetaError): await client.subscribe('token', '789')
    client.call = AsyncMock(side_effect=[{'success': True}, {'data': [{'whatsapp_business_api_data': {'id': '123'}}]}])
    await client.subscribe('token', '789')


async def test_provider_errors_never_echo_credentials(configured, monkeypatch, caplog):
    async def response(*args, **kwargs):
        return httpx.Response(400, json={'error': {'code': 190, 'message': 'private-token'}}, request=httpx.Request('GET', 'https://graph.facebook.com'))
    monkeypatch.setattr(httpx.AsyncClient, 'request', response)
    with pytest.raises(MetaError) as error: await MetaClient().call('GET', 'debug_token', token='private-token')
    assert 'private-token' not in str(error.value)
    with caplog.at_level(logging.INFO, logger='httpx'):
        logging.getLogger('httpx').info('HTTP GET https://graph.facebook.com/debug_token?input_token=private-token')
    assert 'private-token' not in caplog.text
