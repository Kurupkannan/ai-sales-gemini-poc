"""Organizations and their people in Firebase Realtime Database.

Organizations, users, memberships and invitations live here. Leads, calls,
transcripts, audit events and the invitation email queue stay in the local SQL
database and refer to these records by UUID only. Reads are cached briefly
in-process, so per-request access checks and Plivo answer webhooks rarely wait on a
network round trip; every write made through this process invalidates the affected
paths immediately.

Layout:
  organizations/{org_id}                 org_slugs/{slug} -> org_id
  users/{user_id}                        users_by_uid/{firebase_uid} -> user_id
  memberships/{org_id}/{user_id}         user_orgs/{user_id}/{org_id} -> true
  invitations/{org_id}/{invite_id}       invitation_tokens/{token_hash} -> "org_id/invite_id"
                                         invitation_orgs/{invite_id} -> org_id
"""
import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import get_type_hints
from uuid import UUID, uuid4

from app.config import settings

CALL_ROLES = ('org_admin', 'member')


def now():
    return datetime.now(timezone.utc)


@dataclass
class User:
    firebase_uid: str
    email: str
    display_name: str = ''
    active: bool = True
    platform_admin: bool = False
    permissions_version: int = 1
    claims_synced_version: int = 0
    claims_retry_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now)


@dataclass
class Organization:
    name: str
    slug: str
    status: str = 'pending'
    # Auto-dial keeps every caller line busy with not-yet-called leads; calls run as auto_dial_by.
    auto_dial: bool = False
    auto_dial_by: UUID | None = None
    # Set by "Stop all calling": no automatic retries either, until calling is started again.
    calling_paused: bool = False
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now)


@dataclass
class Membership:
    organization_id: UUID
    user_id: UUID
    role: str
    status: str = 'active'
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now)


@dataclass
class Invitation:
    organization_id: UUID
    email: str
    role: str
    token_hash: str
    invited_by: UUID
    expires_at: datetime
    status: str = 'pending'
    accepted_by: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=now)


def dump(record) -> dict:
    data = {}
    for key, value in dataclasses.asdict(record).items():
        if isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.astimezone(timezone.utc).isoformat()
        data[key] = value
    return data


def load(kind, data):
    if not isinstance(data, dict):
        return None
    hints = get_type_hints(kind)
    values = {}
    for item in dataclasses.fields(kind):
        if item.name not in data:
            continue
        value, hint = data[item.name], hints[item.name]
        options = getattr(hint, '__args__', (hint,))
        if value is not None and UUID in options:
            value = UUID(value)
        elif value is not None and datetime in options:
            value = datetime.fromisoformat(value)
        values[item.name] = value
    return kind(**values)


class MemoryBackend:
    """In-process tree with Realtime Database semantics; used by tests."""

    def __init__(self):
        self.root = {}

    def _walk(self, path, create=False):
        node = self.root
        parts = [p for p in path.split('/') if p]
        for part in parts[:-1]:
            if part not in node:
                if not create:
                    return None, parts[-1]
                node[part] = {}
            node = node[part]
        return node, parts[-1]

    def get(self, path):
        if not path.strip('/'):
            return self.root or None
        node, key = self._walk(path)
        return None if node is None else node.get(key)

    def update(self, values: dict):
        for path, value in values.items():
            node, key = self._walk(path, create=True)
            if value is None:
                node.pop(key, None)
            else:
                node[key] = value

    def transaction(self, path, fn):
        result = fn(self.get(path))
        self.update({path: result})
        return result


class RealtimeBackend:
    """Firebase Realtime Database over HTTPS (firebase_admin.db)."""

    def __init__(self):
        from firebase_admin import db
        from app.auth.firebase import firebase_app
        if not settings.FIREBASE_DATABASE_URL:
            raise RuntimeError('FIREBASE_DATABASE_URL is not configured')
        self._root = db.reference('/', app=firebase_app())

    def get(self, path):
        return self._root.child(path.strip('/')).get() if path.strip('/') else self._root.get()

    def update(self, values: dict):
        self._root.update({path.strip('/'): value for path, value in values.items()})

    def transaction(self, path, fn):
        return self._root.child(path.strip('/')).transaction(fn)


class IdentityStore:
    def __init__(self, backend=None, cache_seconds: float | None = None):
        self._backend = backend
        self._cache: dict[str, tuple[float, object]] = {}
        self._ttl = settings.IDENTITY_CACHE_SECONDS if cache_seconds is None else cache_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def backend(self):
        if self._backend is None:
            self._backend = RealtimeBackend()
        return self._backend

    def use(self, backend):
        """Swap the backend (tests); clears cached reads."""
        self._backend = backend
        self._cache.clear()

    def lock(self, key) -> asyncio.Lock:
        """Serializes read-modify-write sequences for one organization/user in this process."""
        return self._locks.setdefault(str(key), asyncio.Lock())

    async def read(self, path, *, fresh=False):
        hit = self._cache.get(path)
        if hit and not fresh and hit[0] > time.monotonic():
            return hit[1]
        value = await asyncio.to_thread(self.backend.get, path)
        if self._ttl:
            self._cache[path] = (time.monotonic() + self._ttl, value)
        return value

    def _invalidate(self, paths):
        for path in paths:
            for cached in list(self._cache):
                if cached.startswith(path) or path.startswith(cached):
                    self._cache.pop(cached, None)

    async def write(self, values: dict):
        """Atomic multi-path update; None deletes a path."""
        self._invalidate(values)
        await asyncio.to_thread(self.backend.update, values)
        self._invalidate(values)

    async def _claim(self, path, value: str) -> str:
        """Set path to value unless it already holds another; returns the owner."""
        self._invalidate([path])
        owner = await asyncio.to_thread(self.backend.transaction, path, lambda current: current or value)
        self._invalidate([path])
        return owner

    # Users -----------------------------------------------------------------
    async def get_user(self, user_id, *, fresh=False) -> User | None:
        return load(User, await self.read(f'users/{user_id}', fresh=fresh))

    async def user_by_uid(self, uid: str, *, fresh=False) -> User | None:
        user_id = await self.read(f'users_by_uid/{uid}', fresh=fresh)
        return await self.get_user(user_id, fresh=fresh) if user_id else None

    async def save_user(self, user: User):
        await self.write({f'users/{user.id}': dump(user), f'users_by_uid/{user.firebase_uid}': str(user.id)})

    async def ensure_user(self, uid: str, email: str, display_name: str = '') -> User:
        """Return the user for a Firebase UID, creating it exactly once."""
        user = await self.user_by_uid(uid)
        if user:
            return user
        candidate = User(firebase_uid=uid, email=email.lower(), display_name=(display_name or '')[:200])
        owner = await self._claim(f'users_by_uid/{uid}', str(candidate.id))
        if owner == str(candidate.id):
            await self.save_user(candidate)
            return candidate
        return await self.get_user(owner, fresh=True) or candidate

    async def all_users(self) -> list[User]:
        users = await self.read('users', fresh=True) or {}
        return [user for user in (load(User, data) for data in users.values()) if user]

    # Organizations ---------------------------------------------------------
    async def get_org(self, org_id, *, fresh=False) -> Organization | None:
        return load(Organization, await self.read(f'organizations/{org_id}', fresh=fresh))

    async def list_orgs(self) -> list[Organization]:
        orgs = await self.read('organizations', fresh=True) or {}
        return sorted((o for o in (load(Organization, d) for d in orgs.values()) if o),
                      key=lambda o: o.created_at, reverse=True)

    async def create_org(self, org: Organization) -> bool:
        """False when the slug is already taken."""
        if await self._claim(f'org_slugs/{org.slug}', str(org.id)) != str(org.id):
            return False
        await self.save_org(org)
        return True

    async def save_org(self, org: Organization):
        await self.write({f'organizations/{org.id}': dump(org)})

    # Memberships -----------------------------------------------------------
    async def membership(self, org_id, user_id, *, fresh=False) -> Membership | None:
        return load(Membership, await self.read(f'memberships/{org_id}/{user_id}', fresh=fresh))

    async def org_members(self, org_id) -> list[Membership]:
        rows = await self.read(f'memberships/{org_id}', fresh=True) or {}
        return [m for m in (load(Membership, d) for d in rows.values()) if m]

    async def user_memberships(self, user_id) -> list[tuple[Organization, Membership]]:
        org_ids = await self.read(f'user_orgs/{user_id}', fresh=True) or {}
        pairs = []
        for org_id in org_ids:
            org, member = await self.get_org(org_id), await self.membership(org_id, user_id)
            if org and member:
                pairs.append((org, member))
        return pairs

    async def save_membership(self, member: Membership):
        await self.write({f'memberships/{member.organization_id}/{member.user_id}': dump(member),
                          f'user_orgs/{member.user_id}/{member.organization_id}': True})

    async def can_call(self, org_id, user_id) -> bool:
        """Active organization, active user and a calling role."""
        org, user, member = await asyncio.gather(
            self.get_org(org_id), self.get_user(user_id), self.membership(org_id, user_id))
        return bool(org and user and member and org.status == 'active' and user.active
                    and member.status == 'active' and member.role in CALL_ROLES)

    # Invitations -----------------------------------------------------------
    async def get_invitation(self, org_id, invite_id) -> Invitation | None:
        return load(Invitation, await self.read(f'invitations/{org_id}/{invite_id}', fresh=True))

    async def invitation_by_id(self, invite_id) -> Invitation | None:
        org_id = await self.read(f'invitation_orgs/{invite_id}', fresh=True)
        return await self.get_invitation(org_id, invite_id) if org_id else None

    async def invitation_by_token(self, token_hash: str) -> Invitation | None:
        location = await self.read(f'invitation_tokens/{token_hash}', fresh=True)
        return load(Invitation, await self.read(f'invitations/{location}', fresh=True)) if location else None

    async def org_invitations(self, org_id) -> list[Invitation]:
        rows = await self.read(f'invitations/{org_id}', fresh=True) or {}
        return sorted((i for i in (load(Invitation, d) for d in rows.values()) if i),
                      key=lambda i: i.created_at, reverse=True)

    async def save_invitation(self, invite: Invitation):
        await self.write({f'invitations/{invite.organization_id}/{invite.id}': dump(invite),
                          f'invitation_tokens/{invite.token_hash}': f'{invite.organization_id}/{invite.id}',
                          f'invitation_orgs/{invite.id}': str(invite.organization_id)})


store = IdentityStore()
