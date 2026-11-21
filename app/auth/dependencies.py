from dataclasses import dataclass
from uuid import UUID
from typing import Annotated
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from firebase_admin import auth as firebase_auth, exceptions as firebase_errors
from google.auth.exceptions import TransportError
from requests.exceptions import RequestException
from app.timing import measure
from app.auth.firebase import verify_token
from app.auth.permissions import ROLES
from app.auth.store import store
from app.config import settings

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
        with measure("auth"):
            claims = await run_in_threadpool(verify_token, authorization[7:])
    except RuntimeError:
        raise HTTPException(503, 'Authentication is not configured.') from None
    except (firebase_auth.CertificateFetchError, firebase_errors.UnavailableError,
            firebase_errors.DeadlineExceededError, firebase_errors.InternalError,
            TransportError, RequestException):
        raise HTTPException(503, 'Authentication service unavailable. Retry shortly.') from None
    except Exception:
        raise HTTPException(401, 'Your session expired or is invalid. Sign in again.') from None
    if not claims.get('email_verified') or not claims.get('email'):
        raise HTTPException(403, 'Verify your email address before continuing.')
    user = await store.ensure_user(claims['uid'], claims['email'], claims.get('name', ''))
    if not user.active:
        raise HTTPException(403, 'Your account is suspended.')
    if user.email != claims['email'].lower():
        user.email = claims['email'].lower()
        await store.save_user(user)
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
    org, member = await store.get_org(org_id), await store.membership(org_id, identity.id)
    if not org or not member or member.status != 'active':
        raise HTTPException(404, 'Organization not found.')
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
