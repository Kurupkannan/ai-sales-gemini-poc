"""Identity, invitation lifecycle, platform administration, and organization RBAC."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID
from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, func
from app.auth.dependencies import CurrentUser, PlatformAdmin, require_permission, recent_login, admin_assurance
from app.auth.permissions import ROLES, PLATFORM_PERMISSIONS
from app.db.models.identity import User, Organization, Membership, Invitation, AuditEvent, EmailOutbox
from app.db.session import database
from app.config import settings

router = APIRouter(prefix='/api', tags=['Identity and organizations'])
Role = Literal['org_admin', 'member', 'viewer']
def now(): return datetime.now(timezone.utc)
def digest(token): return hashlib.sha256(token.encode()).hexdigest()
def org_data(o): return {'id': str(o.id), 'name': o.name, 'slug': o.slug, 'status': o.status}
def audit(s, actor, org_id, action, target, **details):
    s.add(AuditEvent(actor_id=actor.id, organization_id=org_id, action=action, target=str(target), details=details))

@router.get('/me')
async def me(identity: CurrentUser):
    async with database.transaction() as s:
        user = await s.get(User, identity.id)
        rows = (await s.execute(select(Organization, Membership).join(Membership, Membership.organization_id == Organization.id)
            .where(Membership.user_id == identity.id, Membership.status != 'removed').order_by(Organization.name))).all()
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
    async with database.transaction() as s:
        return [org_data(o) for o in (await s.scalars(select(Organization).order_by(Organization.created_at.desc()).limit(500))).all()]

@router.post('/platform/organizations', status_code=201)
async def create_org(body: NewOrg, identity: PlatformAdmin):
    recent_login(identity)
    async with database.transaction() as s:
        if await s.scalar(select(Organization.id).where(Organization.slug == body.slug)):
            raise HTTPException(409, 'This organization slug is already in use.')
        org = Organization(name=body.name.strip(), slug=body.slug)
        s.add(org); await s.flush(); audit(s, identity, org.id, 'organization.created', org.id)
        return org_data(org)

class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    status: Literal['active','suspended','archived'] | None = None

@router.patch('/platform/organizations/{org_id}')
async def update_org(org_id: UUID, body: OrgUpdate, identity: PlatformAdmin):
    recent_login(identity)
    async with database.transaction() as s:
        org = await s.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
        if not org: raise HTTPException(404, 'Organization not found.')
        if body.status == 'active' and not await s.scalar(select(Membership.id).where(
                Membership.organization_id == org_id, Membership.role == 'org_admin', Membership.status == 'active')):
            raise HTTPException(409, 'Invite an administrator before activating this organization.')
        if body.name is not None: org.name = body.name.strip()
        if body.status is not None: org.status = body.status
        audit(s, identity, org_id, 'organization.updated', org_id, **body.model_dump(exclude_none=True))
        return org_data(org)

async def management(s, org_id, identity):
    org = await s.scalar(select(Organization).where(Organization.id == org_id).with_for_update())
    if not org: raise HTTPException(404, 'Organization not found.')
    if not identity.platform_admin:
        member = await s.scalar(select(Membership).where(Membership.organization_id == org_id,
            Membership.user_id == identity.id, Membership.status == 'active', Membership.role == 'org_admin'))
        if not member or org.status != 'active': raise HTTPException(403, 'Organization administrator permission required.')
    admin_assurance(identity)
    return org

class InviteInput(BaseModel):
    email: EmailStr
    role: Role = 'member'

def invite_public(i, delivery=None):
    return {'id': str(i.id), 'email': i.email, 'role': i.role, 'status': 'expired' if i.status == 'pending' and i.expires_at <= now() else i.status,
        'expires_at': i.expires_at, 'delivery': delivery}

async def issue_invitation(s, org, identity, email, role):
    if org.status in ('suspended', 'archived'): raise HTTPException(409, 'Resume the organization before inviting users.')
    email_delivery = bool(settings.SMTP_HOST and settings.SMTP_FROM)
    cipher = None
    if email_delivery:
        try: cipher = Fernet(settings.AUTH_OUTBOX_KEY.get_secret_value().encode())
        except Exception: raise HTTPException(503, 'Invitation encryption is not configured.') from None
    count = await s.scalar(select(func.count()).select_from(Invitation).where(Invitation.organization_id == org.id,
        Invitation.created_at > now()-timedelta(hours=1)))
    if count >= 50: raise HTTPException(429, 'Invitation limit reached. Try again later.')
    existing = (await s.scalars(select(Invitation).where(Invitation.organization_id == org.id,
        Invitation.email == email, Invitation.status == 'pending'))).all()
    for old in existing: old.status = 'revoked'
    await s.flush()
    token = secrets.token_urlsafe(32)
    invite = Invitation(organization_id=org.id, email=email, role=role, token_hash=digest(token),
        invited_by=identity.id, expires_at=now()+timedelta(hours=72), status='pending')
    s.add(invite); await s.flush()
    if cipher:
        s.add(EmailOutbox(invitation_id=invite.id, payload=cipher.encrypt(token.encode()).decode(), next_attempt_at=now()))
    audit(s, identity, org.id, 'invitation.created', invite.id, email=email, role=role)
    result = invite_public(invite, 'pending' if email_delivery else 'manual')
    if not email_delivery:
        result['invitation_url'] = f'{settings.APP_FRONTEND_URL.rstrip("/")}/#invite={token}'
    return result

@router.get('/orgs/{org_id}/invitations')
async def invitations(org_id: UUID, identity: CurrentUser):
    async with database.transaction() as s:
        await management(s, org_id, identity)
        rows = (await s.execute(select(Invitation, EmailOutbox.status).outerjoin(EmailOutbox, EmailOutbox.invitation_id == Invitation.id)
            .where(Invitation.organization_id == org_id).order_by(Invitation.created_at.desc()).limit(100))).all()
        return [invite_public(i, state) for i,state in rows]

@router.post('/orgs/{org_id}/invitations', status_code=201)
async def invite(org_id: UUID, body: InviteInput, identity: CurrentUser):
    recent_login(identity)
    async with database.transaction() as s:
        org = await management(s, org_id, identity)
        return await issue_invitation(s, org, identity, str(body.email).lower(), body.role)

@router.post('/orgs/{org_id}/invitations/{invite_id}/{action}')
async def invitation_action(org_id: UUID, invite_id: UUID, action: Literal['resend','revoke'], identity: CurrentUser):
    recent_login(identity)
    async with database.transaction() as s:
        org = await management(s, org_id, identity)
        row = await s.scalar(select(Invitation).where(Invitation.id == invite_id, Invitation.organization_id == org_id).with_for_update())
        if not row: raise HTTPException(404, 'Invitation not found.')
        if row.status == 'accepted': raise HTTPException(409, 'This invitation was already accepted.')
        row.status = 'revoked'
        audit(s, identity, org_id, 'invitation.'+action, row.id)
        if action == 'resend': return await issue_invitation(s, org, identity, row.email, row.role)
        return {'status': 'revoked'}

class AcceptInvite(BaseModel):
    token: str = Field(min_length=30, max_length=200)

@router.post('/invitations/preview')
async def preview_invitation(body: AcceptInvite, response: Response):
    # The random invitation token authorizes this limited preview, never membership.
    response.headers['Cache-Control'] = 'no-store'
    async with database.transaction() as s:
        row = await s.scalar(select(Invitation).where(Invitation.token_hash == digest(body.token)))
        if not row or row.status != 'pending' or row.expires_at <= now():
            raise HTTPException(400, 'Invitation is invalid or expired.')
        org = await s.get(Organization, row.organization_id)
        if not org or org.status not in ('pending', 'active'):
            raise HTTPException(400, 'Invitation is invalid or expired.')
        return {'email': row.email, 'organization_name': org.name, 'role': row.role}

@router.post('/invitations/accept')
async def accept(body: AcceptInvite, identity: CurrentUser):
    async with database.transaction() as s:
        # Lock organization before invitation, matching management lock order.
        candidate = await s.scalar(select(Invitation).where(Invitation.token_hash == digest(body.token)))
        if not candidate: raise HTTPException(400, 'Invitation is invalid or expired.')
        org = await s.scalar(select(Organization).where(Organization.id == candidate.organization_id).with_for_update())
        row = await s.scalar(select(Invitation).where(Invitation.id == candidate.id).with_for_update().execution_options(populate_existing=True))
        if row.email != identity.email: raise HTTPException(403, 'Sign in with the email address this invitation was sent to.')
        if row.status == 'accepted' and row.accepted_by == identity.id: return org_data(org)
        if row.status != 'pending' or row.expires_at <= now() or org.status not in ('pending','active'):
            raise HTTPException(400, 'Invitation is invalid, expired, or unavailable.')
        inviter = await s.get(User, row.invited_by)
        inviter_member = await s.scalar(select(Membership).where(Membership.organization_id == org.id,
            Membership.user_id == row.invited_by, Membership.role == 'org_admin', Membership.status == 'active'))
        if not inviter or not inviter.active or not (inviter.platform_admin or inviter_member):
            raise HTTPException(403, 'The inviting administrator no longer has access. Request a new invitation.')
        if row.role == 'org_admin': admin_assurance(identity)
        member = await s.scalar(select(Membership).where(Membership.organization_id == org.id, Membership.user_id == identity.id))
        if member and member.status == 'active':
            raise HTTPException(409, 'You are already a member. An administrator can change your role in Team.')
        if not member:
            member = Membership(organization_id=org.id, user_id=identity.id, role=row.role); s.add(member)
        member.role = row.role; member.status = 'active'
        row.status = 'accepted'; row.accepted_by = identity.id
        if org.status == 'pending' and row.role == 'org_admin': org.status = 'active'
        user = await s.scalar(select(User).where(User.id == identity.id).with_for_update()); user.permissions_version += 1
        audit(s, identity, org.id, 'invitation.accepted', row.id, role=row.role)
        return org_data(org)

@router.get('/orgs/{org_id}/members')
async def members(org_id: UUID, identity: CurrentUser):
    async with database.transaction() as s:
        await management(s, org_id, identity)
        rows = (await s.execute(select(Membership, User).join(User, User.id == Membership.user_id)
            .where(Membership.organization_id == org_id).order_by(User.email).limit(500))).all()
        return [{'id': str(m.id), 'email': u.email, 'name': u.display_name, 'role': m.role, 'status': m.status} for m,u in rows]

class MemberUpdate(BaseModel):
    role: Role
    status: Literal['active','suspended','removed']

@router.patch('/orgs/{org_id}/members/{member_id}')
async def update_member(org_id: UUID, member_id: UUID, body: MemberUpdate, identity: CurrentUser):
    recent_login(identity)
    async with database.transaction() as s:
        await management(s, org_id, identity)
        m = await s.scalar(select(Membership).where(Membership.id == member_id, Membership.organization_id == org_id))
        if not m: raise HTTPException(404, 'Member not found.')
        if m.role == 'org_admin' and m.status == 'active' and (body.role != 'org_admin' or body.status != 'active'):
            admins = await s.scalar(select(func.count()).select_from(Membership).join(User, User.id == Membership.user_id).where(Membership.organization_id == org_id,
                Membership.role == 'org_admin', Membership.status == 'active', User.active.is_(True)))
            if admins <= 1: raise HTTPException(409, 'Keep at least one active organization administrator.')
        before = {'role': m.role, 'status': m.status}
        m.role = body.role; m.status = body.status
        u = await s.scalar(select(User).where(User.id == m.user_id).with_for_update()); u.permissions_version += 1
        audit(s, identity, org_id, 'membership.updated', m.id, before=before, after=body.model_dump())
        return {'status': 'updated'}

@router.get('/orgs/{org_id}/audit-events')
async def audit_events(org_id: UUID, identity: CurrentUser):
    async with database.transaction() as s:
        await management(s, org_id, identity)
        rows = (await s.scalars(select(AuditEvent).where(AuditEvent.organization_id == org_id)
            .order_by(AuditEvent.created_at.desc()).limit(100))).all()
        return [{'id': str(r.id), 'action': r.action, 'target': r.target, 'details': r.details, 'created_at': r.created_at} for r in rows]

@router.patch('/orgs/{org_id}/settings')
async def org_settings(org_id: UUID, body: NewOrg, identity: CurrentUser):
    recent_login(identity)
    async with database.transaction() as s:
        o = await management(s, org_id, identity)
        o.name = body.name.strip()  # Slugs are immutable once created.
        audit(s, identity, org_id, 'organization.renamed', o.id, name=o.name)
        return org_data(o)
