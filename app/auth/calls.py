"""Organization-bound, one-use media authorization for outbound calls."""
import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID
from fastapi import HTTPException
from sqlalchemy import select, func
from app.config import settings
from app.db.session import database
from app.db.models import LeadContact, ContactPreference
from app.db.models.identity import OutboundAuthorization, Organization, Membership, User

def now(): return datetime.now(timezone.utc)
def digest(token): return hashlib.sha256(token.encode()).hexdigest()

async def authorize_call(org_id, identity, payload):
    if str(org_id) != settings.EXOTEL_ORGANIZATION_ID:
        raise HTTPException(503, 'An Exotel account has not been assigned to this organization.')
    try: contact_id = UUID(payload.contact_id)
    except ValueError: raise HTTPException(422, 'Invalid contact ID.') from None
    async with database.transaction() as s:
        s.info['organization_id'] = org_id
        await s.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
        if await s.scalar(select(OutboundAuthorization.id).where(OutboundAuthorization.organization_id == org_id,
                OutboundAuthorization.idempotency_key == payload.idempotency_key)):
            raise HTTPException(409, 'This call request was already submitted. Do not retry an uncertain provider result.')
        contact = await s.get(LeadContact, contact_id)
        preference = await s.scalar(select(ContactPreference).where(ContactPreference.contact_id == contact_id))
        if not contact: raise HTTPException(404, 'Contact not found.')
        if not preference or preference.suppressed or contact.channel != 'voice':
            raise HTTPException(403, 'This contact is not eligible for voice outreach.')
        if contact.phone_number != payload.to_number: raise HTTPException(422, 'Number does not match the selected contact.')
        count = await s.scalar(select(func.count()).select_from(OutboundAuthorization).where(
            OutboundAuthorization.organization_id == org_id, OutboundAuthorization.created_at > now()-timedelta(hours=1)))
        if count >= 30: raise HTTPException(429, 'Hourly call limit reached.')
        token = secrets.token_urlsafe(32)
        s.add(OutboundAuthorization(organization_id=org_id, actor_id=identity.id, token_hash=digest(token),
            expires_at=now()+timedelta(minutes=5), idempotency_key=payload.idempotency_key))
    return token

async def active(s, row):
    org = await s.get(Organization, row.organization_id)
    user = await s.get(User, row.actor_id)
    member = await s.scalar(select(Membership).where(Membership.organization_id == row.organization_id,
        Membership.user_id == row.actor_id, Membership.status == 'active', Membership.role.in_(['org_admin','member'])))
    return org and org.status == 'active' and user and user.active and member

async def claim_stream(websocket):
    token = websocket.query_params.get('token', '')
    # Uvicorn logs the scope during accept/close. Never include stream credentials.
    websocket.scope['query_string'] = b''
    if len(token) < 30 or len(token) > 200: return None
    async with database.transaction() as s:
        row = await s.scalar(select(OutboundAuthorization).where(OutboundAuthorization.token_hash == digest(token)).with_for_update())
        if not row or row.state == 'failed' or row.used or row.expires_at <= now() or not await active(s, row): return None
        row.used = True
        return row.id

async def monitor_stream(websocket, ident, handler):
    task = asyncio.create_task(handler(websocket))
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=15)
            if done: break
            async with database.transaction() as s:
                row = await s.get(OutboundAuthorization, ident)
                if not row or not await active(s, row):
                    await websocket.close(code=1008); task.cancel(); break
        await task
    finally:
        if not task.done(): task.cancel()


async def finish_call(token, state, provider_id=None):
    from app.db.models.identity import AuditEvent
    async with database.transaction() as s:
        row = await s.scalar(select(OutboundAuthorization).where(OutboundAuthorization.token_hash == digest(token)))
        if row:
            row.state = state; row.provider_call_id = provider_id
            s.add(AuditEvent(organization_id=row.organization_id, actor_id=row.actor_id,
                action='call.'+state, target=str(row.id), details={}))
