from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi import HTTPException, Response
from pydantic import SecretStr
from app.api import identity
from app.auth.store import Organization, Invitation
from app.db.models.identity import EmailOutbox


@pytest.fixture
def local_rows(monkeypatch):
    added = []
    @asynccontextmanager
    async def transaction():
        yield SimpleNamespace(add=added.append)
    monkeypatch.setattr(identity.database, 'transaction', transaction)
    return added


async def test_manual_invitation_without_smtp_or_encryption(monkeypatch, memory_store, local_rows):
    monkeypatch.setattr(identity.settings, 'SMTP_HOST', '')
    monkeypatch.setattr(identity.settings, 'SMTP_FROM', '')
    monkeypatch.setattr(identity.settings, 'AUTH_OUTBOX_KEY', SecretStr(''))
    monkeypatch.setattr(identity.settings, 'APP_FRONTEND_URL', 'http://localhost:5173/ui/')
    org = Organization(name='Acme', slug='acme')
    result = await identity.issue_invitation(org, SimpleNamespace(id=uuid4()), 'invited@example.com', 'org_admin')
    assert result['delivery'] == 'manual'
    token = result['invitation_url'].split('#invite=')[1]
    invitation = await memory_store.invitation_by_token(identity.digest(token))
    assert invitation.email == 'invited@example.com' and invitation.organization_id == org.id
    assert await memory_store.invitation_by_id(invitation.id) == invitation
    assert not any(isinstance(row, EmailOutbox) for row in local_rows)
    assert 'invitation_url' not in identity.invite_public(invitation)
    assert token not in repr(identity.invite_public(invitation))


@pytest.mark.parametrize('status,expired,org_status,valid', [
    ('pending',False,'active',True), ('revoked',False,'active',False),
    ('accepted',False,'active',False), ('pending',True,'active',False),
    ('pending',False,'suspended',False),
])
async def test_invitation_preview(memory_store, status, expired, org_status, valid):
    org = Organization(name='Acme', slug='acme', status=org_status)
    await memory_store.save_org(org)
    token = 'x' * 43
    await memory_store.save_invitation(Invitation(organization_id=org.id, email='invited@example.com',
        role='org_admin', token_hash=identity.digest(token), invited_by=uuid4(), status=status,
        expires_at=identity.now() + timedelta(hours=-1 if expired else 1)))
    response = Response()
    if valid:
        result = await identity.preview_invitation(identity.AcceptInvite(token=token), response)
        assert result == {'email': 'invited@example.com', 'organization_name': 'Acme', 'role': 'org_admin'}
        assert response.headers['Cache-Control'] == 'no-store'
    else:
        with pytest.raises(HTTPException) as exc:
            await identity.preview_invitation(identity.AcceptInvite(token=token), response)
        assert exc.value.status_code == 400
