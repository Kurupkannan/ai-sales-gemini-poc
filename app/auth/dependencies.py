from dataclasses import dataclass
from uuid import UUID
from typing import Annotated
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from starlette.concurrency import run_in_threadpool
from app.auth.firebase import verify_token
from app.auth.permissions import ROLES
from app.config import settings
from app.db.session import database
from app.db.models.identity import User, Organization, Membership

@dataclass
class Identity:
    id: UUID
    uid: str
    email: str
    platform_admin: bool
    claims: dict

def admin_assurance(identity: Identity):
    if settings.AUTH_REQUIRE_ADMIN_MFA and not identity.claims.get('firebase', {}).get('sign_in_second_factor'):
        raise HTTPException(403, 'Enroll and sign in with two-factor authentication to administer this workspace.')

def recent_login(identity: Identity):
    import time
    if time.time() - identity.claims.get('auth_time', 0) > 600:
        raise HTTPException(403, 'Sign out and sign in again before changing access or organization settings.')

async def current_user(request: Request):
    authorization = request.headers.get('authorization', '')
    if not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Sign in to continue.', headers={'WWW-Authenticate': 'Bearer'})
    try:
        claims = await run_in_threadpool(verify_token, authorization[7:])
    except RuntimeError:
        raise HTTPException(503, 'Authentication is not configured.') from None
    except Exception:
        raise HTTPException(401, 'Your session expired or is invalid. Sign in again.') from None
    if not claims.get('email_verified') or not claims.get('email'):
        raise HTTPException(403, 'Verify your email address before continuing.')
    async with database.transaction() as session:
        unsafe = await session.scalar(text("SELECT rolsuper OR rolbypassrls OR rolcreaterole OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='app' AND pg_has_role(current_user,c.relowner,'MEMBER')) FROM pg_roles WHERE rolname=current_user"))
        if unsafe:
            raise HTTPException(503, 'Configure the restricted application database role before enabling sign-in.')
        await session.execute(insert(User).values(firebase_uid=claims['uid'], email=claims['email'].lower(),
            display_name=claims.get('name', '')[:200]).on_conflict_do_nothing(index_elements=[User.firebase_uid]))
        user = await session.scalar(select(User).where(User.firebase_uid == claims['uid']))
        if not user.active:
            raise HTTPException(403, 'Your account is suspended.')
        user.email = claims['email'].lower()
        identity = Identity(user.id, user.firebase_uid, user.email, user.platform_admin, claims)
    request.state.identity = identity
    return identity

CurrentUser = Annotated[Identity, Depends(current_user)]

async def platform_admin(identity: CurrentUser):
    if not identity.platform_admin:
        raise HTTPException(403, 'Platform administrator permission required.')
    admin_assurance(identity)
    return identity

PlatformAdmin = Annotated[Identity, Depends(platform_admin)]

async def organization_access(request: Request, identity: CurrentUser):
    try: org_id = UUID(request.path_params['org_id'])
    except (ValueError, KeyError): raise HTTPException(404, 'Organization not found.') from None
    async with database.transaction() as session:
        org = await session.get(Organization, org_id)
        member = await session.scalar(select(Membership).where(Membership.organization_id == org_id,
            Membership.user_id == identity.id, Membership.status == 'active'))
        if not org or not member: raise HTTPException(404, 'Organization not found.')
        if org.status != 'active': raise HTTPException(403, 'This organization is not active.')
        if member.role == 'org_admin': admin_assurance(identity)
        permissions = ROLES.get(member.role, frozenset())
    request.state.organization_id = org_id
    request.state.permissions = permissions
    return identity

OrgUser = Annotated[Identity, Depends(organization_access)]

def require_permission(permission):
    async def dependency(request: Request, identity: OrgUser):
        if permission not in request.state.permissions:
            raise HTTPException(403, 'You do not have permission for this action.')
        return identity
    return dependency
