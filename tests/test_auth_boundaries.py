"""Unauthenticated access must fail before any business database/provider calls."""
from unittest.mock import AsyncMock
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from app.main import app
from app.auth import dependencies
from app.auth.permissions import ROLES

ORG = '10000000-0000-4000-8000-000000000001'

@pytest.mark.parametrize('method,path', [
    ('GET', '/api/me'), ('GET','/api/platform/organizations'),
    ('POST','/api/platform/organizations'), ('GET',f'/api/orgs/{ORG}/leads'),
    ('GET',f'/api/orgs/{ORG}/imports'), ('GET',f'/api/orgs/{ORG}/members'),
    ('POST',f'/api/orgs/{ORG}/invitations'), ('GET',f'/api/orgs/{ORG}/audit-events'),
    ('POST',f'/api/orgs/{ORG}/calls'), ('GET',f'/api/orgs/{ORG}/whatsapp/status'),
    ('POST',f'/api/orgs/{ORG}/whatsapp/session'), ('POST','/api/invitations/accept'),
])
async def test_missing_token_rejected(method, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.request(method,path)
        assert response.status_code == 401, response.text
        assert response.headers['cache-control'] == 'no-store'

@pytest.mark.parametrize('path', ['/api/leads','/api/imports','/api/whatsapp/status','/test-call'])
async def test_legacy_routes_removed(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get(path)).status_code == 404

@pytest.mark.parametrize('reason', ['expired','revoked','wrong-project','forged'])
async def test_verification_errors_deny_access(monkeypatch, reason):
    def reject(token): raise ValueError(reason)
    monkeypatch.setattr(dependencies, 'verify_token', reject)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/me', headers={'Authorization':'Bearer bad-token'})
        assert response.status_code == 401
        assert response.json()["detail"] == "Your session expired or is invalid. Sign in again."

async def test_unverified_email_cannot_provision_user(monkeypatch):
    monkeypatch.setattr(dependencies, 'verify_token', lambda token: {'uid':'u', 'email':'u@example.com', 'email_verified':False})
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/me',headers={'Authorization':'Bearer token'})).status_code == 403

def test_anonymous_media_stream_rejected():
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect('/exotel/media'): pass
        assert exc.value.code == 1008

def test_permission_matrix():
    assert 'members.manage' not in ROLES['member']
    assert ROLES['viewer'] == {'leads.read'}
    assert 'organizations.manage' not in ROLES['org_admin']
    assert ROLES['viewer'] < ROLES['member'] < ROLES['org_admin']
