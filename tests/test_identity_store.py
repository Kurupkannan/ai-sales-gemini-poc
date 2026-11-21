"""Organizations and their people live in Realtime Database; calls check them via the store."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.auth import calls
from app.auth.store import IdentityStore, MemoryBackend, Membership, Organization


async def test_user_is_created_once_per_firebase_uid():
    store = IdentityStore(MemoryBackend(), cache_seconds=30)
    first, second = await asyncio.gather(store.ensure_user('uid-1', 'A@Example.com'),
                                         store.ensure_user('uid-1', 'a@example.com'))
    assert first.id == second.id and first.email == 'a@example.com'
    assert len(await store.all_users()) == 1


async def test_org_slug_is_unique_and_roundtrips():
    store = IdentityStore(MemoryBackend())
    org = Organization(name='Trade Max', slug='trade-max', status='active')
    assert await store.create_org(org)
    assert not await store.create_org(Organization(name='Other', slug='trade-max'))
    assert await store.get_org(org.id) == org


async def test_call_permission_follows_membership_and_cache_invalidation(monkeypatch):
    store = IdentityStore(MemoryBackend(), cache_seconds=300)
    monkeypatch.setattr(calls, 'store', store)
    org = Organization(name='Trade Max', slug='trade-max', status='active')
    await store.create_org(org)
    user = await store.ensure_user('uid-1', 'agent@example.com')
    row = SimpleNamespace(organization_id=org.id, actor_id=user.id)
    assert not await calls.active(None, row)
    member = Membership(organization_id=org.id, user_id=user.id, role='member')
    await store.save_membership(member)
    assert await calls.active(None, row)
    assert [o.id for o, _ in await store.user_memberships(user.id)] == [org.id]
    member.role = 'viewer'  # Viewers may read leads but never place calls.
    await store.save_membership(member)
    assert not await calls.active(None, row)
    member.role, org.status = 'member', 'suspended'
    await store.save_membership(member)
    await store.save_org(org)
    assert not await calls.active(None, row)


def test_new_numbers_are_callable_unless_permission_is_required(monkeypatch):
    from app.services import leads
    monkeypatch.setattr(leads.settings, 'CALLS_REQUIRE_PERMISSION', False)
    assert leads.default_preference() == {'suppressed': False, 'reason': 'allowed_on_import'}
    monkeypatch.setattr(leads.settings, 'CALLS_REQUIRE_PERMISSION', True)
    assert leads.default_preference()['suppressed'] is True


def test_opt_out_and_wrong_number_block_future_calls():
    assert set(calls.BLOCKING_OUTCOMES) == {'DO_NOT_CALL', 'WRONG_NUMBER'}


async def test_auto_dial_skips_inactive_organizations(monkeypatch):
    from app.telephony import dialer
    queried = []
    async def candidates(*args):
        queried.append(args)
        return []
    monkeypatch.setattr(dialer, 'auto_candidates', candidates)
    await dialer.auto_dial_org(Organization(name='Acme', slug='acme', status='suspended', auto_dial=True, auto_dial_by=uuid4()))
    await dialer.auto_dial_org(Organization(name='Acme', slug='acme', status='active', calling_paused=True))
    assert queried == []  # Suspended, or paused by "Stop all calling": no retries either.
    # Active organizations are checked every round: for due retries even with auto-dial off.
    await dialer.auto_dial_org(Organization(name='Acme', slug='acme', status='active'))
    assert len(queried) == 1


def test_stop_all_cancels_every_queued_call_for_the_organization():
    from app.telephony import dialer
    org, other = uuid4(), uuid4()
    mine = dialer.Batch(uuid4(), org, dialer.runtime.now(), [{'lead_id': '1', 'status': 'queued'}, {'lead_id': '2', 'status': 'active'}])
    theirs = dialer.Batch(uuid4(), other, dialer.runtime.now(), [{'lead_id': '3', 'status': 'queued'}])
    dialer._batches.update({mine.id: mine, theirs.id: theirs})
    try:
        assert dialer.stop_all(org) == 1
        assert [i['status'] for i in mine.items] == ['cancelled', 'active']  # Connected call untouched.
        assert theirs.items[0]['status'] == 'queued'  # Other organizations unaffected.
    finally:
        dialer._batches.pop(mine.id); dialer._batches.pop(theirs.id)
