"""Concurrent batch dialer: keeps every caller number busy until the queue is empty.

Each worker holds one line (caller number) from authorization until the call has been
finalized, then the next queued lead takes it. Batches live in memory; a restart ends
them, while calls already placed are reconciled by the telephony worker as usual.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.auth import calls
from app.config import settings
from app.db.models import ContactPreference, LeadContact
from app.telephony import runtime
from app.telephony.service import initiate

logger = logging.getLogger(__name__)
ACTIVE = {'queued', 'waiting', 'dialing', 'pending', 'initiated', 'active', 'unknown'}


@dataclass
class Batch:
    id: UUID
    organization_id: UUID
    created_at: datetime
    items: list[dict]
    cancelled: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)

    def snapshot(self):
        counts = {}
        for item in self.items:
            counts[item['status']] = counts.get(item['status'], 0) + 1
        return {'batch_id': str(self.id), 'created_at': self.created_at, 'capacity': settings.call_capacity,
                'done': not any(item['status'] in ACTIVE for item in self.items),
                'cancelled': self.cancelled, 'counts': counts,
                'items': [{k: v for k, v in item.items() if k != 'to_number'} for item in self.items]}


_batches: dict[UUID, Batch] = {}


def get(org_id: UUID, batch_id: UUID) -> Batch:
    batch = _batches.get(batch_id)
    if not batch or batch.organization_id != org_id:
        raise HTTPException(404, 'Call batch not found.')
    return batch


async def start(org_id: UUID, identity, lead_ids: list[UUID]) -> dict:
    lead_ids = list(dict.fromkeys(lead_ids))
    async with calls.database.transaction() as s:
        await calls.tenant(s, org_id)
        contacts = (await s.execute(select(LeadContact)
            .join(ContactPreference, ContactPreference.contact_id == LeadContact.id)
            .where(LeadContact.organization_id == org_id, LeadContact.lead_id.in_(lead_ids),
                   LeadContact.channel == 'voice', ContactPreference.suppressed.is_(False))
            .order_by(LeadContact.created_at))).scalars().all()
    contact_for = {}
    for contact in contacts:
        contact_for.setdefault(contact.lead_id, contact)
    # Leads waiting or dialling in any running batch (button, auto-dial, retries) are not added twice.
    queued = {item['lead_id'] for batch in _batches.values() if batch.organization_id == org_id
              for item in batch.items if item['status'] in ACTIVE}
    items = []
    for lead_id in lead_ids:
        item = {'lead_id': str(lead_id), 'status': 'queued', 'call_id': None, 'detail': None}
        contact = contact_for.get(lead_id)
        if contact is None:
            item.update(status='skipped', detail='No voice contact with calling permission.')
        elif any(live.ended_at is None for live in runtime.for_lead(org_id, lead_id)):
            item.update(status='skipped', detail='A call is already in progress for this lead.')
        elif str(lead_id) in queued:
            item.update(status='skipped', detail='Already queued in another batch.')
        else:
            item.update(contact_id=str(contact.id), to_number=contact.phone_number)
        items.append(item)
    batch = Batch(uuid4(), org_id, runtime.now(), items)
    _batches[batch.id] = batch
    for old in sorted(_batches.values(), key=lambda b: b.created_at)[:-50]:
        if old.task is None or old.task.done():
            _batches.pop(old.id, None)
    batch.task = asyncio.create_task(_run(batch, identity), name=f'call-batch-{batch.id}')
    logger.info('Call batch started batch=%s leads=%s capacity=%s', batch.id, len(items), settings.call_capacity)
    return batch.snapshot()


def cancel(batch: Batch) -> dict:
    batch.cancelled = True
    for item in batch.items:
        if item['status'] in {'queued', 'waiting'}:
            item.update(status='cancelled', detail='Cancelled before dialing.')
    return batch.snapshot()


async def _run(batch: Batch, identity):
    lines = asyncio.Semaphore(settings.call_capacity)
    await asyncio.gather(*(_one(batch, item, identity, lines)
                           for item in batch.items if item['status'] == 'queued'))
    logger.info('Call batch finished batch=%s', batch.id)


async def _one(batch: Batch, item: dict, identity, lines: asyncio.Semaphore):
    async with lines:
        payload = SimpleNamespace(contact_id=item['contact_id'], to_number=item['to_number'],
                                  idempotency_key=f"batch-{batch.id}-{item['lead_id']}")
        while True:
            if batch.cancelled:
                return
            item['status'] = 'dialing'
            try:
                result = await initiate(batch.organization_id, identity, payload)
                break
            except HTTPException as exc:
                retry = (exc.headers or {}).get('Retry-After')
                if exc.status_code == 429 and retry:
                    item.update(status='waiting', detail=exc.detail)
                    await asyncio.sleep(int(retry))
                    continue
                item.update(status='failed', detail=exc.detail)
                return
            except Exception:
                logger.exception('Batch dial failed batch=%s lead=%s', batch.id, item['lead_id'])
                item.update(status='failed', detail='Unexpected error while dialing.')
                return
        item.update(call_id=result['call_id'], status=result['status'], detail=result.get('end_reason'))
        live = runtime.get(UUID(result['attempt_id']))
        # Hold this line until the call is persisted, so its number is free for the next lead.
        while live is not None and runtime.get(live.id) is live:
            item['status'] = live.state
            await asyncio.sleep(2)
        if live is not None:
            item.update(status=live.state, detail=live.end_reason)


# Auto-dial and retries ---------------------------------------------------
# Every round, per active organization:
#  - retries: leads whose every attempt so far ended NOT_ANSWERED / NO_RESPONSE, with at most
#    CALL_RETRY_MAX attempts after the first, CALL_RETRY_AFTER_MINUTES after the last one;
#  - fresh leads (never dialled), only while the organization has auto_dial on.
# A lead anyone actually talked to, or a blocked number, is never redialled automatically.
RETRYABLE = ('NOT_ANSWERED', 'NO_RESPONSE')
_auto: dict[UUID, list[Batch]] = {}


def _callable(org_id: UUID):
    from app.db.models import Lead
    return (select(Lead.id)
        .join(LeadContact, LeadContact.lead_id == Lead.id)
        .join(ContactPreference, ContactPreference.contact_id == LeadContact.id)
        .where(Lead.organization_id == org_id, LeadContact.channel == 'voice',
               ContactPreference.suppressed.is_(False)))


def _never_called(org_id: UUID):
    from app.db.models import Conversation, Lead
    attempted = select(Conversation.lead_id).where(Conversation.organization_id == org_id)
    return _callable(org_id).where(Lead.status == 'new', Lead.id.not_in(attempted))


def _attempts(org_id: UUID):
    from sqlalchemy import case, func
    from app.db.models import CallSession, Conversation
    outcome = CallSession.result_summary['lead_status'].as_string()
    return (select(Conversation.lead_id.label('lead_id'), func.count().label('tries'),
                   func.sum(case((outcome.in_(RETRYABLE), 1), else_=0)).label('missed'),
                   func.max(Conversation.created_at).label('last'))
            .join(CallSession, CallSession.conversation_id == Conversation.id)
            .where(Conversation.organization_id == org_id)
            .group_by(Conversation.lead_id).subquery())


def _retry_due(org_id: UUID, *, due_only=True):
    from datetime import timedelta
    from app.db.models import Lead
    tries = _attempts(org_id)
    query = _callable(org_id).join(tries, tries.c.lead_id == Lead.id).where(
        tries.c.tries == tries.c.missed, tries.c.tries <= settings.CALL_RETRY_MAX)
    if due_only:
        query = query.where(tries.c.last <= runtime.now() - timedelta(minutes=settings.CALL_RETRY_AFTER_MINUTES))
    return query


def _last_actor():
    from app.db.models import CallSession, Conversation, Lead
    from app.db.models.identity import OutboundAuthorization
    return (select(OutboundAuthorization.actor_id)
        .join(CallSession, CallSession.id == OutboundAuthorization.call_session_id)
        .join(Conversation, Conversation.id == CallSession.conversation_id)
        .where(Conversation.lead_id == Lead.id)
        .order_by(OutboundAuthorization.created_at.desc()).limit(1)
        .correlate(Lead).scalar_subquery())


async def auto_candidates(org, limit: int) -> list[tuple[UUID, UUID]]:
    """(lead_id, actor_id) pairs to dial now: due retries first, then fresh leads if auto-dial is on."""
    from app.db.models import Lead
    async with calls.database.transaction() as s:
        await calls.tenant(s, org.id)
        retry_ids = select(_retry_due(org.id).subquery().c.id)
        rows = (await s.execute(select(Lead.id, _last_actor()).where(Lead.id.in_(retry_ids))
                                .order_by(Lead.created_at, Lead.id).limit(limit))).all()
        picked = [(lead_id, org.auto_dial_by or actor) for lead_id, actor in rows if org.auto_dial_by or actor]
        if org.auto_dial and org.auto_dial_by and len(picked) < limit:
            fresh = select(_never_called(org.id).subquery().c.id)
            picked += [(lead_id, org.auto_dial_by) for lead_id in (await s.scalars(
                select(Lead.id).where(Lead.id.in_(fresh)).order_by(Lead.created_at, Lead.id)
                .limit(limit - len(picked)))).all()]
    return picked


async def never_called_ids(org_id: UUID, limit: int = 500) -> list[UUID]:
    from app.db.models import Lead
    async with calls.database.transaction() as s:
        await calls.tenant(s, org_id)
        fresh = select(_never_called(org_id).subquery().c.id)
        return list((await s.scalars(select(Lead.id).where(Lead.id.in_(fresh))
                                     .order_by(Lead.created_at, Lead.id).limit(limit))).all())


async def auto_status(org) -> dict:
    from sqlalchemy import func
    async with calls.database.transaction() as s:
        await calls.tenant(s, org.id)
        waiting = await s.scalar(select(func.count()).select_from(_never_called(org.id).subquery()))
        retry_due = await s.scalar(select(func.count()).select_from(_retry_due(org.id).subquery()))
        retry_later = await s.scalar(select(func.count()).select_from(_retry_due(org.id, due_only=False).subquery()))
    batches = _auto.get(org.id) or []
    live = sum(1 for item in runtime._calls.values()
               if item.organization_id == org.id and item.ended_at is None)
    return {'enabled': org.auto_dial, 'paused': org.calling_paused, 'capacity': settings.call_capacity, 'active_calls': live,
            'waiting_leads': waiting, 'retry_due': retry_due, 'retry_scheduled': retry_later - retry_due,
            'retry_max': settings.CALL_RETRY_MAX, 'retry_after_minutes': settings.CALL_RETRY_AFTER_MINUTES,
            'batch': batches[-1].snapshot() if batches else None}


def stop_all(org_id: UUID) -> int:
    """Cancel every queued (not yet dialled) call for an organization; connected calls continue."""
    cancelled = 0
    for batch in _batches.values():
        if batch.organization_id == org_id:
            before = sum(item['status'] in {'queued', 'waiting'} for item in batch.items)
            cancel(batch)
            cancelled += before
    return cancelled


def stop_auto(org_id: UUID):
    """Stops fresh auto-dial; retries already queued in this round are cancelled too."""
    for batch in _auto.get(org_id) or []:
        cancel(batch)


async def auto_dial_org(org):
    """Start the next round for one organization unless its current round is still running."""
    if org.status != 'active' or org.calling_paused:
        return
    if any(batch.task and not batch.task.done() for batch in _auto.get(org.id) or []):
        return
    picked = await auto_candidates(org, settings.AUTO_DIAL_BATCH_SIZE)
    if not picked:
        return
    by_actor: dict[UUID, list[UUID]] = {}
    for lead_id, actor in picked:
        by_actor.setdefault(actor, []).append(lead_id)
    batches = []
    for actor, lead_ids in by_actor.items():
        snapshot = await start(org.id, SimpleNamespace(id=actor), lead_ids)
        batches.append(_batches[UUID(snapshot['batch_id'])])
    _auto[org.id] = batches


async def auto_dial_loop():
    from app.auth.store import store
    while True:
        try:
            if settings.OUTBOUND_CALLS_ENABLED:
                for org in await store.list_orgs():
                    await auto_dial_org(org)
        except Exception:
            logger.exception('Auto-dial round failed; retrying')
        await asyncio.sleep(settings.AUTO_DIAL_INTERVAL_SECONDS)
