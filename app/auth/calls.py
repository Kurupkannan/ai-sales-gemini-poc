"""Tenant-bound outbound attempts, media grants and idempotent call lifecycle."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import phonenumbers
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select, func, text, or_

from app.config import settings
from app.db.session import database
from app.db.models import Lead, LeadContact, ContactPreference, Conversation, CallSession, TranscriptTurn
from app.db.models.identity import OutboundAuthorization, AuditEvent
from app.auth.store import store

TERMINAL = {'completed', 'failed'}
BLOCKING_OUTCOMES = {'DO_NOT_CALL': 'customer_opted_out', 'WRONG_NUMBER': 'wrong_number'}


def now():
    return datetime.now(timezone.utc)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def cipher():
    return Fernet(settings.MEDIA_GRANT_ENCRYPTION_KEY.get_secret_value().encode())


def normalize_number(value: str, *, callback=False) -> str:
    if callback and value.isdigit():
        value = '+' + value
    try:
        number = phonenumbers.parse(value, None)
        if not value.startswith('+') or not phonenumbers.is_valid_number(number):
            raise ValueError()
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    except (ValueError, phonenumbers.NumberParseException):
        raise HTTPException(422, 'Use a valid international phone number with +countrycode.') from None


async def tenant(session, org_id):
    session.info['organization_id'] = org_id
    # Authorization lookup may already have opened the transaction.
    if not database.is_sqlite:
        await session.execute(text("SELECT set_config('app.organization_id', :org, true)"), {'org': str(org_id)})


async def active(session, row):
    """The caller still belongs to an active organization with a calling role."""
    return await store.can_call(row.organization_id, row.actor_id)


async def authorize_call(org_id, identity, payload):
    if str(org_id) != settings.PLIVO_ORGANIZATION_ID:
        raise HTTPException(503, 'A Plivo account has not been assigned to this organization.')
    number = normalize_number(payload.to_number)
    country = phonenumbers.region_code_for_number(phonenumbers.parse(number, None))
    if country not in {c.strip().upper() for c in settings.PLIVO_ALLOWED_COUNTRIES.split(',')}:
        raise HTTPException(403, 'Calls to this country are disabled.')
    try:
        contact_id = UUID(payload.contact_id)
    except ValueError:
        raise HTTPException(422, 'Invalid contact ID.') from None
    async with database.transaction() as s:
        s.info['organization_id'] = org_id
        if not database.is_sqlite:  # SQLite: BEGIN IMMEDIATE already serializes number selection.
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:org, 0))"), {'org': str(org_id)})
        if await s.scalar(select(OutboundAuthorization.id).where(
                OutboundAuthorization.organization_id == org_id,
                OutboundAuthorization.idempotency_key == payload.idempotency_key)):
            raise HTTPException(409, 'This call request was already submitted. Check its status; do not redial an uncertain result.')
        context = (await s.execute(select(LeadContact, ContactPreference, Lead)
            .outerjoin(ContactPreference, ContactPreference.contact_id == LeadContact.id)
            .outerjoin(Lead, Lead.id == LeadContact.lead_id)
            .where(LeadContact.id == contact_id))).first()
        if not context:
            raise HTTPException(404, 'Contact not found.')
        contact, preference, lead = context
        if not preference or preference.suppressed or contact.channel != 'voice':
            raise HTTPException(403, 'This contact is not eligible for voice outreach.')
        if contact.phone_number != number:
            raise HTTPException(422, 'Number does not match the selected contact.')
        if not lead:
            raise HTTPException(404, 'Lead not found.')
        previous = (await s.execute(select(OutboundAuthorization, CallSession)
            .join(CallSession, CallSession.id == OutboundAuthorization.call_session_id)
            .join(Conversation, Conversation.id == CallSession.conversation_id)
            .where(OutboundAuthorization.organization_id == org_id,
                   Conversation.lead_id == contact.lead_id)
            .order_by(OutboundAuthorization.created_at.desc()).limit(19))).all()
        prior_history = [{**snapshot(item), 'created_at': item.created_at,
            'answered_at': item.answered_at, 'ended_at': session.ended_at,
            'bill_duration': session.bill_duration, 'summary': session.result_summary}
            for item, session in previous]
        condition = OutboundAuthorization.organization_id == org_id
        # The organization lock (BEGIN IMMEDIATE on SQLite) makes number selection race-free.
        hourly = await s.scalar(select(func.count()).where(
            condition, OutboundAuthorization.created_at > now() - timedelta(hours=1)))
        if hourly >= settings.TELEPHONY_HOURLY_CALL_LIMIT:
            raise HTTPException(429, 'Hourly call limit reached.')
        busy = dict((await s.execute(select(OutboundAuthorization.from_number, func.count()).where(
            condition, OutboundAuthorization.provider == 'plivo',
            or_(OutboundAuthorization.state.not_in(TERMINAL), OutboundAuthorization.cleanup_needed),
        ).group_by(OutboundAuthorization.from_number))).all())
        free = [n for n in settings.from_numbers if busy.get(n, 0) < settings.TELEPHONY_CALLS_PER_NUMBER]
        if sum(busy.values()) >= settings.call_capacity or not free:
            # Retry-After marks this as "wait for a line", unlike the hourly limit.
            raise HTTPException(429, 'All calling numbers are busy; try again shortly.', headers={'Retry-After': '5'})
        from_number = min(free, key=lambda n: busy.get(n, 0))
        conversation = Conversation(id=uuid4(), organization_id=org_id, contact_id=contact.id,
            lead_id=contact.lead_id, channel='voice', status='pending')
        s.add(conversation)
        await s.flush()
        call = CallSession(id=uuid4(), organization_id=org_id, conversation_id=conversation.id,
            provider='plivo', provider_account_id=settings.PLIVO_AUTH_ID,
            idempotency_key=payload.idempotency_key, status='pending')
        s.add(call)
        await s.flush()
        row = OutboundAuthorization(id=uuid4(), organization_id=org_id, actor_id=identity.id,
            call_session_id=call.id, provider='plivo', provider_account_id=settings.PLIVO_AUTH_ID,
            from_number=from_number, to_number=number,
            token_hash=digest(secrets.token_urlsafe(32)), idempotency_key=payload.idempotency_key,
            expires_at=now() + timedelta(seconds=settings.CALL_ANSWER_TIMEOUT_SECONDS + 60),
            reconcile_after=now() + timedelta(seconds=30), state='pending', used=False)
        if not await active(s, row):
            raise HTTPException(403, 'Calling access is no longer active.')
        s.add(row)
        s.add(AuditEvent(organization_id=org_id, actor_id=identity.id, action='call.pending', target=str(row.id), details={}))
    # This immutable context is copied into memory before contacting Plivo.
    row.conversation_id = conversation.id
    row.lead_id = contact.lead_id
    row.contact_id = contact.id
    row.lead_name = lead.name
    row.lead_source = lead.source
    row.preferred_language = lead.preferred_language
    row.prior_history = prior_history
    return row


async def locked_attempt(s, ident, *, set_tenant=True):
    row = await s.scalar(select(OutboundAuthorization).where(
        OutboundAuthorization.id == ident, OutboundAuthorization.provider == 'plivo',
        OutboundAuthorization.provider_account_id == settings.PLIVO_AUTH_ID,
        OutboundAuthorization.organization_id == UUID(settings.PLIVO_ORGANIZATION_ID)).with_for_update())
    if not row:
        raise HTTPException(404, 'Call attempt not found.')
    if set_tenant:
        await tenant(s, row.organization_id)
    return row


async def sync_call(s, row, *, provider_status=None, bill_duration=None):
    call = await s.get(CallSession, row.call_session_id)
    call.status = row.state
    call.provider_request_id = row.provider_request_id
    call.provider_call_id = row.provider_call_id
    call.stream_id = row.stream_id
    call.end_reason = row.end_reason
    if provider_status:
        call.provider_status = provider_status[:100]
    if bill_duration is not None:
        call.bill_duration = bill_duration
    if row.answered_at:
        call.started_at = row.answered_at
    conversation = await s.get(Conversation, call.conversation_id)
    if row.state in TERMINAL:
        call.ended_at = call.ended_at or now()
        conversation.ended_at = conversation.ended_at or now()
        conversation.status = 'completed' if row.state == 'completed' else 'failed'
        if row.state == 'completed' and row.answered_at:
            lead = await s.get(Lead, conversation.lead_id)
            if lead and lead.status == 'new':
                lead.status = 'contacted'
        row.token_ciphertext = None
    elif row.state == 'active':
        conversation.status = 'active'
        conversation.started_at = conversation.started_at or row.answered_at


async def transition(s, row, state, reason=None, *, sync=True):
    # Never regress a confirmed call when the create response/callback arrives late.
    if row.state in TERMINAL or (row.state == 'active' and state in {'pending', 'initiated', 'unknown'}):
        return
    if row.state != state:
        row.state = state
        s.add(AuditEvent(organization_id=row.organization_id, actor_id=row.actor_id,
            action='call.' + state, target=str(row.id), details={'reason': reason} if reason else {}))
    if reason:
        row.end_reason = reason[:200]
    if sync:
        await sync_call(s, row)


async def dial_result(ident, *, request_id=None, state='initiated'):
    async with database.transaction() as s:
        row = await locked_attempt(s, ident)
        if request_id:
            if row.provider_request_id and row.provider_request_id != request_id:
                raise HTTPException(409, 'Provider request identity mismatch.')
            row.provider_request_id = request_id
        await transition(s, row, state, 'dial_rejected' if state == 'failed' else None)
        await sync_call(s, row)
        if state == 'unknown' and row.state == 'unknown':
            row.reconcile_after = now() + timedelta(seconds=30)
        return snapshot(row)


def snapshot(row):
    return {'call_id': str(row.call_session_id), 'attempt_id': str(row.id), 'provider': 'plivo',
        'status': row.state, 'provider_request_id': row.provider_request_id,
        'provider_call_id': row.provider_call_id, 'end_reason': row.end_reason,
        'cleanup_pending': row.cleanup_needed}


async def read_call(org_id, call_id):
    from app.telephony import runtime
    live = runtime.by_call(org_id, call_id)
    if live:
        return live.snapshot()
    async with database.transaction() as s:
        row = await s.scalar(select(OutboundAuthorization).where(
            OutboundAuthorization.organization_id == org_id,
            OutboundAuthorization.call_session_id == call_id, OutboundAuthorization.provider == 'plivo'))
        if not row:
            raise HTTPException(404, 'Call not found.')
        result = snapshot(row)
        await tenant(s, org_id)
        call = await s.get(CallSession, call_id)
        result['summary'] = call.result_summary if call else None
        return result


async def calls_for_lead(org_id, lead_id):
    from app.telephony import runtime
    live = runtime.for_lead(org_id, lead_id)
    if live:
        latest = max(live, key=lambda item: item.created_at)
        return [item.snapshot() for item in sorted(live, key=lambda item: item.created_at, reverse=True)] + latest.prior_history
    async with database.transaction() as s:
        await tenant(s, org_id)
        lead = await s.get(Lead, lead_id)
        if not lead:
            raise HTTPException(404, 'Lead not found.')
        rows = (await s.execute(select(OutboundAuthorization, CallSession)
            .join(CallSession, CallSession.id == OutboundAuthorization.call_session_id)
            .join(Conversation, Conversation.id == CallSession.conversation_id)
            .where(OutboundAuthorization.organization_id == org_id,
                   OutboundAuthorization.provider == 'plivo', Conversation.lead_id == lead_id)
            .order_by(OutboundAuthorization.created_at.desc()).limit(20))).all()
        return [{**snapshot(attempt), 'created_at': attempt.created_at,
                 'answered_at': attempt.answered_at, 'ended_at': call.ended_at,
                 'provider_status': call.provider_status, 'bill_duration': call.bill_duration,
                 'summary': call.result_summary}
                for attempt, call in rows]


async def persist_finished_call(live):
    """One durable transaction after Plivo has ended the call."""
    async with database.transaction() as s:
        row = await locked_attempt(s, live.id)
        call = await s.get(CallSession, row.call_session_id)
        if call.result_summary is not None:
            return
        row.provider_request_id = live.provider_request_id
        row.provider_call_id = live.provider_call_id
        row.stream_id = live.stream_id
        row.answered_at = live.answered_at
        row.heartbeat_at = live.last_activity
        row.used = live.used
        row.state = live.state
        row.end_reason = live.end_reason
        row.cleanup_needed = False
        row.reconcile_after = None
        row.token_ciphertext = None
        await sync_call(s, row, bill_duration=live.bill_duration)
        call.ended_at = live.ended_at or now()
        call.result_summary = live.summary
        conversation = await s.get(Conversation, live.conversation_id)
        conversation.ended_at = live.ended_at or now()
        for sequence, (speaker, value, occurred_at) in enumerate(live.transcript):
            s.add(TranscriptTurn(organization_id=live.organization_id,
                conversation_id=live.conversation_id, call_session_id=live.call_session_id,
                sequence=sequence, speaker=speaker, text=value, occurred_at=occurred_at,
                interrupted=False))
        s.add(AuditEvent(organization_id=live.organization_id, actor_id=live.actor_id,
            action='call.finalized', target=str(live.id), details={'status': live.state}))
        # Numbers are callable without prior permission, so an opt-out heard on the call must stick.
        outcome = (live.summary or {}).get('lead_status')
        if outcome in BLOCKING_OUTCOMES:
            preference = await s.scalar(select(ContactPreference).where(
                ContactPreference.contact_id == live.contact_id,
                ContactPreference.organization_id == live.organization_id))
            if preference and not preference.suppressed:
                preference.suppressed = True
                preference.reason = BLOCKING_OUTCOMES[outcome]


def bind_callback(row, form, *, stream=False):
    if form.get('Direction') != 'outbound' and not (stream and 'Direction' not in form):
        raise HTTPException(403, 'Only authorized outbound calls are supported.')
    try:
        call_id = str(UUID(form.get('CallUUID', '')))
    except ValueError:
        raise HTTPException(400, 'Invalid call identifier.') from None
    if row.provider_call_id and row.provider_call_id != call_id:
        raise HTTPException(403, 'Call identity mismatch.')
    if stream and not row.provider_call_id:
        raise HTTPException(403, 'Call has not been bound.')
    for key, expected in [('From', row.from_number), ('To', row.to_number)]:
        if (not stream or key in form) and normalize_number(form.get(key, ''), callback=True) != expected:
            raise HTTPException(403, 'Call number mismatch.')
    request_id = form.get('RequestUUID')
    if request_id:
        try:
            request_id = str(UUID(request_id))
        except ValueError:
            raise HTTPException(400, 'Invalid request identifier.') from None
        if row.provider_request_id and row.provider_request_id != request_id:
            raise HTTPException(403, 'Request identity mismatch.')
        row.provider_request_id = request_id
    row.provider_call_id = call_id


async def callback(ident, kind, form):
    """Row locking + monotonic state make repeated callbacks side-effect idempotent."""
    async with database.transaction() as s:
        row = await locked_attempt(s, ident, set_tenant=kind != 'answer')
        newly_bound = row.provider_call_id is None
        bind_callback(row, form, stream=kind == 'stream-status')
        token = None
        if kind == 'hangup':
            answer_time = None
            try:
                answer_time = datetime.fromisoformat(form.get('AnswerTime', '').replace('Z', '+00:00'))
                if answer_time.tzinfo is None:
                    answer_time = answer_time.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass
            answered = bool(row.answered_at or answer_time)
            if answered and not row.answered_at:
                row.answered_at = min(answer_time, now())
            # Socket close commonly races the authoritative hangup callback.
            # Correct only that provisional disconnect failure, never a known bridge error.
            if answered and row.state == 'failed' and row.end_reason == 'media_disconnected':
                row.state = 'active'
            await transition(s, row, 'completed' if answered else 'failed', form.get('HangupCauseName') or form.get('CallStatus') or 'hangup')
            row.cleanup_needed = False
            row.reconcile_after = None
        elif kind == 'answer':
            if row.state in TERMINAL or row.cleanup_needed or (not row.answered_at and row.expires_at <= now()) or not await active(s, row):
                await tenant(s, row.organization_id)
                row.cleanup_needed = True
                row.reconcile_after = now()
                await transition(s, row, 'failed', 'answer_not_authorized')
            else:
                if not row.answered_at:
                    row.answered_at = now()
                    token = secrets.token_urlsafe(32)
                    row.token_hash = digest(token)
                    row.token_ciphertext = cipher().encrypt(token.encode()).decode()
                    row.expires_at = now() + timedelta(seconds=settings.MEDIA_GRANT_TTL_SECONDS)
                elif row.expires_at > now() and row.token_ciphertext:
                    token = cipher().decrypt(row.token_ciphertext.encode()).decode()
                if token:
                    # Commit the media grant quickly. start_stream records the
                    # CallSession/Conversation after Plivo opens the socket.
                    await transition(s, row, 'active', sync=False)
                else:
                    await tenant(s, row.organization_id)
                    row.cleanup_needed = True
                    row.reconcile_after = now()
                    await transition(s, row, 'failed', 'media_grant_expired')
        elif kind == 'ring':
            if row.state == 'failed' and newly_bound:
                # A delayed ringing callback can reveal a call after local cancellation.
                row.cleanup_needed = True
                row.reconcile_after = now()
            await transition(s, row, 'initiated')
        elif kind == 'stream-status':
            stream_id = form.get('StreamID', '')
            if not stream_id or len(stream_id) > 200 or (row.stream_id and row.stream_id != stream_id):
                raise HTTPException(403, 'Stream identity mismatch.')
            row.stream_id = stream_id
            event = form.get('Event', '')
            if event in {'stopped', 'failed', 'timeout'} and row.state not in TERMINAL:
                row.cleanup_needed = True
                row.reconcile_after = now()
                await transition(s, row, 'failed', 'stream_' + event)
        duration = form.get('BillDuration', '')
        if kind != 'answer' or not token:
            await sync_call(s, row, provider_status=form.get('CallStatus'),
                bill_duration=int(duration) if duration.isdigit() and len(duration) < 10 else None)
        return token


async def claim_stream(token):
    if not 30 <= len(token) <= 200:
        return None
    async with database.transaction() as s:
        row = await s.scalar(select(OutboundAuthorization)
            .where(OutboundAuthorization.token_hash == digest(token),
                   OutboundAuthorization.provider == 'plivo',
                   OutboundAuthorization.provider_account_id == settings.PLIVO_AUTH_ID,
                   OutboundAuthorization.organization_id == UUID(settings.PLIVO_ORGANIZATION_ID),
                   OutboundAuthorization.used.is_(False),
                   OutboundAuthorization.state == 'active',
                   OutboundAuthorization.cleanup_needed.is_(False),
                   OutboundAuthorization.expires_at > now())
            .with_for_update())
        if not row or not await active(s, row):
            return None
        row.used = True
        row.heartbeat_at = now()
        return row


async def start_stream(ident, stream_id):
    async with database.transaction() as s:
        row = await locked_attempt(s, ident)
        if row.state != 'active' or row.cleanup_needed or (row.stream_id and row.stream_id != stream_id):
            raise ValueError('Stream is no longer active.')
        row.stream_id = stream_id
        row.heartbeat_at = now()
        await sync_call(s, row)


async def heartbeat(ident):
    async with database.transaction() as s:
        row = await locked_attempt(s, ident)
        if row.state != 'active' or row.cleanup_needed or not await active(s, row):
            return False
        row.heartbeat_at = now()
        return True


async def request_cleanup(ident, reason):
    async with database.transaction() as s:
        row = await locked_attempt(s, ident)
        if row.state in TERMINAL and not row.cleanup_needed:
            return None
        row.cleanup_needed = True
        row.reconcile_after = now()
        await transition(s, row, 'failed', reason)
        return row


async def release_unconfirmed(ident):
    """Close an attempt Plivo never confirmed so its caller number can be used again."""
    async with database.transaction() as s:
        row = await locked_attempt(s, ident)
        if row.state in TERMINAL or row.provider_call_id or row.provider_request_id:
            return
        await transition(s, row, 'failed', 'dial_unconfirmed', sync=row.call_session_id is not None)
        row.cleanup_needed = False
        row.reconcile_after = None


async def cleanup_complete(ident, *, reconcile=True):
    async with database.transaction() as s:
        row = await locked_attempt(s, ident)
        row.cleanup_needed = False
        row.reconcile_after = (now() + timedelta(seconds=30)) if reconcile and row.provider_call_id else None
