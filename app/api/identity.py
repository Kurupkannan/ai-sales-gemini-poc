"""Identity, invitation lifecycle, platform administration, and organization RBAC.

Organizations, users, memberships and invitations live in Firebase Realtime Database
(app.auth.store); the audit trail and invitation email queue stay in the local database.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from app.auth.dependencies import CurrentUser, PlatformAdmin, recent_login, admin_assurance
from app.auth.permissions import ROLES, PLATFORM_PERMISSIONS
from app.auth.store import store, Organization, Membership, Invitation
from app.db.models.identity import AuditEvent, EmailOutbox
from app.db.session import database
from app.config import settings

router = APIRouter(prefix='/api', tags=['Identity and organizations'])
Role = Literal['org_admin', 'member', 'viewer']
def now(): return datetime.now(timezone.utc)
def digest(token): return hashlib.sha256(token.encode()).hexdigest()
def org_data(o): return {'id': str(o.id), 'name': o.name, 'slug': o.slug, 'status': o.status}

async def audit(actor, org_id, action, target, **details):
    async with database.transaction() as s:
        s.add(AuditEvent(actor_id=actor.id, organization_id=org_id, action=action, target=str(target), details=details))

async def bump_permissions(user_id):
    user = await store.get_user(user_id, fresh=True)
    if user:
        user.permissions_version += 1
        await store.save_user(user)

@router.get('/me')
async def me(identity: CurrentUser):
    user = await store.get_user(identity.id, fresh=True)
    rows = sorted([(o, m) for o, m in await store.user_memberships(identity.id) if m.status != 'removed'],
                  key=lambda pair: pair[0].name)
    return {'id': str(user.id), 'email': user.email, 'display_name': user.display_name,
        'platform_admin': user.platform_admin, 'permissions_version': user.permissions_version,
        'claims_pending': user.claims_synced_version != user.permissions_version,
        'platform_permissions': PLATFORM_PERMISSIONS if user.platform_admin else [],
        'admin_mfa_required': settings.AUTH_REQUIRE_ADMIN_MFA,
        'organizations': [{**org_data(o), 'role': m.role, 'membership_status': m.status,
            'permissions': sorted(ROLES[m.role]) if m.status == 'active' and o.status == 'active' else []} for o,m in rows]}

class NewOrg(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    slug: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{1,79}$')

@router.get('/platform/organizations')
async def organizations(identity: PlatformAdmin):
    return [org_data(o) for o in (await store.list_orgs())[:500]]

@router.post('/platform/organizations', status_code=201)
async def create_org(body: NewOrg, identity: PlatformAdmin):
    recent_login(identity)
    org = Organization(name=body.name.strip(), slug=body.slug)
    if not await store.create_org(org):
        raise HTTPException(409, 'This organization slug is already in use.')
    await audit(identity, org.id, 'organization.created', org.id)
    return org_data(org)

class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    status: Literal['active','suspended','archived'] | None = None

@router.patch('/platform/organizations/{org_id}')
async def update_org(org_id: UUID, body: OrgUpdate, identity: PlatformAdmin):
    recent_login(identity)
    async with store.lock(org_id):
        org = await store.get_org(org_id, fresh=True)
        if not org: raise HTTPException(404, 'Organization not found.')
        if body.status == 'active' and not any(m.role == 'org_admin' and m.status == 'active'
                                               for m in await store.org_members(org_id)):
            raise HTTPException(409, 'Invite an administrator before activating this organization.')
        if body.name is not None: org.name = body.name.strip()
        if body.status is not None: org.status = body.status
        await store.save_org(org)
    await audit(identity, org_id, 'organization.updated', org_id, **body.model_dump(exclude_none=True))
    return org_data(org)

async def management(org_id, identity):
    org = await store.get_org(org_id, fresh=True)
    if not org: raise HTTPException(404, 'Organization not found.')
    if not identity.platform_admin:
        member = await store.membership(org_id, identity.id, fresh=True)
        if (not member or member.status != 'active' or member.role != 'org_admin'
                or org.status != 'active'):
            raise HTTPException(403, 'Organization administrator permission required.')
    admin_assurance(identity)
    return org

class InviteInput(BaseModel):
    email: EmailStr
    role: Role = 'member'

def invite_public(i, delivery=None):
    return {'id': str(i.id), 'email': i.email, 'role': i.role, 'status': 'expired' if i.status == 'pending' and i.expires_at <= now() else i.status,
        'expires_at': i.expires_at, 'delivery': delivery}

async def issue_invitation(org, identity, email, role):
    if org.status in ('suspended', 'archived'): raise HTTPException(409, 'Resume the organization before inviting users.')
    email_delivery = bool(settings.SMTP_HOST and settings.SMTP_FROM)
    cipher = None
    if email_delivery:
        try: cipher = Fernet(settings.AUTH_OUTBOX_KEY.get_secret_value().encode())
        except Exception: raise HTTPException(503, 'Invitation encryption is not configured.') from None
    existing = await store.org_invitations(org.id)
    if sum(i.created_at > now()-timedelta(hours=1) for i in existing) >= 50:
        raise HTTPException(429, 'Invitation limit reached. Try again later.')
    for old in existing:
        if old.email == email and old.status == 'pending':
            old.status = 'revoked'
            await store.save_invitation(old)
    token = secrets.token_urlsafe(32)
    invite = Invitation(organization_id=org.id, email=email, role=role, token_hash=digest(token),
        invited_by=identity.id, expires_at=now()+timedelta(hours=72), status='pending')
    await store.save_invitation(invite)
    async with database.transaction() as s:
        if cipher:
            s.add(EmailOutbox(invitation_id=invite.id, payload=cipher.encrypt(token.encode()).decode(), next_attempt_at=now()))
        s.add(AuditEvent(actor_id=identity.id, organization_id=org.id, action='invitation.created',
            target=str(invite.id), details={'email': email, 'role': role}))
    result = invite_public(invite, 'pending' if email_delivery else 'manual')
    if not email_delivery:
        result['invitation_url'] = f'{settings.APP_FRONTEND_URL.rstrip("/")}/#invite={token}'
    return result

@router.get('/orgs/{org_id}/invitations')
async def invitations(org_id: UUID, identity: CurrentUser):
    await management(org_id, identity)
    rows = (await store.org_invitations(org_id))[:100]
    async with database.transaction() as s:
        delivery = dict((await s.execute(select(EmailOutbox.invitation_id, EmailOutbox.status)
            .where(EmailOutbox.invitation_id.in_([i.id for i in rows])))).all()) if rows else {}
    return [invite_public(i, delivery.get(i.id)) for i in rows]

@router.post('/orgs/{org_id}/invitations', status_code=201)
async def invite(org_id: UUID, body: InviteInput, identity: CurrentUser):
    recent_login(identity)
    async with store.lock(org_id):
        org = await management(org_id, identity)
        return await issue_invitation(org, identity, str(body.email).lower(), body.role)

@router.post('/orgs/{org_id}/invitations/{invite_id}/{action}')
async def invitation_action(org_id: UUID, invite_id: UUID, action: Literal['resend','revoke'], identity: CurrentUser):
    recent_login(identity)
    async with store.lock(org_id):
        org = await management(org_id, identity)
        row = await store.get_invitation(org_id, invite_id)
        if not row: raise HTTPException(404, 'Invitation not found.')
        if row.status == 'accepted': raise HTTPException(409, 'This invitation was already accepted.')
        row.status = 'revoked'
        await store.save_invitation(row)
        await audit(identity, org_id, 'invitation.'+action, row.id)
        if action == 'resend': return await issue_invitation(org, identity, row.email, row.role)
        return {'status': 'revoked'}

class AcceptInvite(BaseModel):
    token: str = Field(min_length=30, max_length=200)

@router.post('/invitations/preview')
async def preview_invitation(body: AcceptInvite, response: Response):
    # The random invitation token authorizes this limited preview, never membership.
    response.headers['Cache-Control'] = 'no-store'
    row = await store.invitation_by_token(digest(body.token))
    if not row or row.status != 'pending' or row.expires_at <= now():
        raise HTTPException(400, 'Invitation is invalid or expired.')
    org = await store.get_org(row.organization_id, fresh=True)
    if not org or org.status not in ('pending', 'active'):
        raise HTTPException(400, 'Invitation is invalid or expired.')
    return {'email': row.email, 'organization_name': org.name, 'role': row.role}

@router.post('/invitations/accept')
async def accept(body: AcceptInvite, identity: CurrentUser):
    candidate = await store.invitation_by_token(digest(body.token))
    if not candidate: raise HTTPException(400, 'Invitation is invalid or expired.')
    async with store.lock(candidate.organization_id):
        org = await store.get_org(candidate.organization_id, fresh=True)
        row = await store.get_invitation(candidate.organization_id, candidate.id)
        if row.email != identity.email: raise HTTPException(403, 'Sign in with the email address this invitation was sent to.')
        if row.status == 'accepted' and row.accepted_by == identity.id: return org_data(org)
        if not org or row.status != 'pending' or row.expires_at <= now() or org.status not in ('pending','active'):
            raise HTTPException(400, 'Invitation is invalid, expired, or unavailable.')
        inviter = await store.get_user(row.invited_by, fresh=True)
        inviter_member = await store.membership(org.id, row.invited_by, fresh=True)
        inviter_admin = inviter_member and inviter_member.role == 'org_admin' and inviter_member.status == 'active'
        if not inviter or not inviter.active or not (inviter.platform_admin or inviter_admin):
            raise HTTPException(403, 'The inviting administrator no longer has access. Request a new invitation.')
        if row.role == 'org_admin': admin_assurance(identity)
        member = await store.membership(org.id, identity.id, fresh=True)
        if member and member.status == 'active':
            raise HTTPException(409, 'You are already a member. An administrator can change your role in Team.')
        if not member:
            member = Membership(organization_id=org.id, user_id=identity.id, role=row.role)
        member.role = row.role; member.status = 'active'
        await store.save_membership(member)
        row.status = 'accepted'; row.accepted_by = identity.id
        await store.save_invitation(row)
        if org.status == 'pending' and row.role == 'org_admin':
            org.status = 'active'
            await store.save_org(org)
        await bump_permissions(identity.id)
        await audit(identity, org.id, 'invitation.accepted', row.id, role=row.role)
        return org_data(org)

@router.get('/orgs/{org_id}/members')
async def members(org_id: UUID, identity: CurrentUser):
    await management(org_id, identity)
    rows = []
    for m in await store.org_members(org_id):
        u = await store.get_user(m.user_id)
        if u: rows.append({'id': str(m.id), 'email': u.email, 'name': u.display_name, 'role': m.role, 'status': m.status})
    return sorted(rows, key=lambda r: r['email'])[:500]

class MemberUpdate(BaseModel):
    role: Role
    status: Literal['active','suspended','removed']

@router.patch('/orgs/{org_id}/members/{member_id}')
async def update_member(org_id: UUID, member_id: UUID, body: MemberUpdate, identity: CurrentUser):
    recent_login(identity)
    async with store.lock(org_id):
        await management(org_id, identity)
        everyone = await store.org_members(org_id)
        m = next((item for item in everyone if item.id == member_id), None)
        if not m: raise HTTPException(404, 'Member not found.')
        if m.role == 'org_admin' and m.status == 'active' and (body.role != 'org_admin' or body.status != 'active'):
            admins = 0
            for item in everyone:
                if item.role == 'org_admin' and item.status == 'active':
                    user = await store.get_user(item.user_id)
                    admins += bool(user and user.active)
            if admins <= 1: raise HTTPException(409, 'Keep at least one active organization administrator.')
        before = {'role': m.role, 'status': m.status}
        m.role = body.role; m.status = body.status
        await store.save_membership(m)
        await bump_permissions(m.user_id)
        await audit(identity, org_id, 'membership.updated', m.id, before=before, after=body.model_dump())
        return {'status': 'updated'}

@router.get('/orgs/{org_id}/audit-events')
async def audit_events(org_id: UUID, identity: CurrentUser):
    await management(org_id, identity)
    async with database.transaction() as s:
        rows = (await s.scalars(select(AuditEvent).where(AuditEvent.organization_id == org_id)
            .order_by(AuditEvent.created_at.desc()).limit(100))).all()
        return [{'id': str(r.id), 'action': r.action, 'target': r.target, 'details': r.details, 'created_at': r.created_at} for r in rows]

@router.patch('/orgs/{org_id}/settings')
async def org_settings(org_id: UUID, body: NewOrg, identity: CurrentUser):
    recent_login(identity)
    async with store.lock(org_id):
        o = await management(org_id, identity)
        o.name = body.name.strip()  # Slugs are immutable once created.
        await store.save_org(o)
    await audit(identity, org_id, 'organization.renamed', o.id, name=o.name)
    return org_data(o)
