from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from pydantic import SecretStr
from app.api import identity
from app.db.models.identity import Invitation, EmailOutbox

@pytest.mark.asyncio
async def test_manual_invitation_without_smtp_or_encryption(monkeypatch):
    monkeypatch.setattr(identity.settings, 'SMTP_HOST', '')
    monkeypatch.setattr(identity.settings, 'SMTP_FROM', '')
    monkeypatch.setattr(identity.settings, 'AUTH_OUTBOX_KEY', SecretStr(''))
    monkeypatch.setattr(identity.settings, 'APP_FRONTEND_URL', 'http://localhost:5173/ui/')
    added = []
    session = SimpleNamespace(scalar=AsyncMock(return_value=0),
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
        flush=AsyncMock(), add=added.append)
    result = await identity.issue_invitation(session,
        SimpleNamespace(id=uuid4(), status='pending'), SimpleNamespace(id=uuid4()),
        'invited@example.com', 'org_admin')
    assert result['delivery'] == 'manual'
    token = result['invitation_url'].split('#invite=')[1]
    invitation = next(row for row in added if isinstance(row, Invitation))
    assert invitation.token_hash == identity.digest(token)
    assert invitation.email == 'invited@example.com'
    assert not any(isinstance(row, EmailOutbox) for row in added)
    assert 'invitation_url' not in identity.invite_public(invitation)
    assert token not in repr(identity.invite_public(invitation))

@pytest.mark.asyncio
@pytest.mark.parametrize('status,expired,org_status,valid', [
    ('pending',False,'active',True), ('revoked',False,'active',False),
    ('accepted',False,'active',False), ('pending',True,'active',False),
    ('pending',False,'suspended',False),
])
async def test_invitation_preview(monkeypatch,status,expired,org_status,valid):
    from contextlib import asynccontextmanager
    from datetime import timedelta
    from fastapi import Response, HTTPException
    row=SimpleNamespace(email='invited@example.com',role='org_admin',organization_id=uuid4(),
        status=status,expires_at=identity.now()+timedelta(hours=-1 if expired else 1))
    session=SimpleNamespace(scalar=AsyncMock(return_value=row),
        get=AsyncMock(return_value=SimpleNamespace(name='Acme',status=org_status)))
    @asynccontextmanager
    async def transaction(): yield session
    monkeypatch.setattr(identity.database,'transaction',transaction)
    response=Response()
    if valid:
        result=await identity.preview_invitation(identity.AcceptInvite(token='x'*43),response)
        assert result == {'email':'invited@example.com','organization_name':'Acme','role':'org_admin'}
        assert response.headers['Cache-Control']=='no-store'
    else:
        with pytest.raises(HTTPException) as exc:
            await identity.preview_invitation(identity.AcceptInvite(token='x'*43),response)
        assert exc.value.status_code==400
