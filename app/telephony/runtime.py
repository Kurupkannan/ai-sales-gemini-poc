"""Single-process, database-free state for an in-progress outbound call.

The complete authorization and lead context is loaded before dialing. A process
restart ends any live media session; the durable pending row is then reconciled
from Plivo after the call, without pretending the audio session survived.
"""
import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException

from app.config import settings
from app.timing import call_mark

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc)


@dataclass
class LiveCall:
    id: UUID
    organization_id: UUID
    actor_id: UUID
    call_session_id: UUID
    conversation_id: UUID
    lead_id: UUID
    contact_id: UUID
    lead_name: str | None
    lead_source: str | None
    preferred_language: str | None
    provider_account_id: str
    from_number: str
    to_number: str
    created_at: datetime
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    state: str = 'pending'
    provider_request_id: str | None = None
    provider_call_id: str | None = None
    stream_id: str | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None
    used: bool = False
    cleanup_needed: bool = False
    bill_duration: int | None = None
    summary: dict | None = None
    audio: object | None = field(default=None, repr=False)
    transcript: list[tuple[str, str, datetime]] = field(default_factory=list)
    prior_history: list[dict] = field(default_factory=list)
    last_activity: datetime = field(default_factory=now)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    finalize_task: asyncio.Task | None = field(default=None, repr=False)

    def snapshot(self):
        return {
            'call_id': str(self.call_session_id), 'attempt_id': str(self.id),
            'provider': 'plivo', 'status': self.state,
            'provider_request_id': self.provider_request_id,
            'provider_call_id': self.provider_call_id,
            'end_reason': self.end_reason, 'cleanup_pending': self.cleanup_needed,
            'created_at': self.created_at, 'answered_at': self.answered_at,
            'ended_at': self.ended_at, 'bill_duration': self.bill_duration,
            'summary': self.summary,
        }

    def add_transcript(self, speaker: str, value: str):
        text = value.strip()
        if self.transcript and self.transcript[-1][0] == speaker and self.transcript[-1][1] == text:
            return
        if text and len(self.transcript) < 400:
            self.transcript.append((speaker, text[:2000], now()))


_calls: dict[UUID, LiveCall] = {}


def register(row) -> LiveCall:
    call = LiveCall(
        id=row.id, organization_id=row.organization_id, actor_id=row.actor_id,
        call_session_id=row.call_session_id, conversation_id=row.conversation_id,
        lead_id=row.lead_id, contact_id=row.contact_id,
        lead_name=row.lead_name, lead_source=row.lead_source,
        preferred_language=row.preferred_language,
        provider_account_id=row.provider_account_id,
        from_number=row.from_number, to_number=row.to_number,
        created_at=row.created_at or now(),
        prior_history=getattr(row, 'prior_history', []),
    )
    _calls[call.id] = call
    return call


def get(attempt_id: UUID) -> LiveCall | None:
    return _calls.get(attempt_id)


def by_call(org_id: UUID, call_id: UUID) -> LiveCall | None:
    return next((item for item in _calls.values()
                 if item.organization_id == org_id and item.call_session_id == call_id), None)


def for_lead(org_id: UUID, lead_id: UUID) -> list[LiveCall]:
    return [item for item in _calls.values()
            if item.organization_id == org_id and item.lead_id == lead_id]


def in_progress() -> bool:
    return any(item.ended_at is None for item in _calls.values())


def bind(call: LiveCall, form: dict, *, stream=False):
    from app.auth.calls import normalize_number
    if form.get('Direction') != 'outbound' and not (stream and 'Direction' not in form):
        raise HTTPException(403, 'Only authorized outbound calls are supported.')
    try:
        call_id = str(UUID(form.get('CallUUID', '')))
    except ValueError:
        raise HTTPException(400, 'Invalid call identifier.') from None
    if call.provider_call_id and call.provider_call_id != call_id:
        raise HTTPException(403, 'Call identity mismatch.')
    if stream and not call.provider_call_id:
        raise HTTPException(403, 'Call has not been bound.')
    for key, expected in [('From', call.from_number), ('To', call.to_number)]:
        if (not stream or key in form) and normalize_number(form.get(key, ''), callback=True) != expected:
            raise HTTPException(403, 'Call number mismatch.')
    request_id = form.get('RequestUUID')
    if request_id:
        try:
            request_id = str(UUID(request_id))
        except ValueError:
            raise HTTPException(400, 'Invalid request identifier.') from None
        if call.provider_request_id and call.provider_request_id != request_id:
            raise HTTPException(403, 'Request identity mismatch.')
        call.provider_request_id = request_id
    call.provider_call_id = call_id


async def callback(attempt_id: UUID, kind: str, form: dict) -> str | None:
    call = get(attempt_id)
    if not call:
        raise HTTPException(404, 'Live call not found.')
    async with call.lock:
        bind(call, form, stream=kind == 'stream-status')
        call.last_activity = now()
        if kind == 'ring':
            if call.state == 'pending':
                call.state = 'initiated'
        elif kind == 'answer':
            if call.ended_at or call.cleanup_needed or now() > call.created_at + timedelta(
                    seconds=settings.CALL_ANSWER_TIMEOUT_SECONDS + 60):
                return None
            call_mark(call, 'answer')
            call.answered_at = call.answered_at or now()
            call.state = 'active'
            return call.token
        elif kind == 'stream-status':
            stream_id = form.get('StreamID', '')
            if not stream_id or len(stream_id) > 200 or (call.stream_id and call.stream_id != stream_id):
                raise HTTPException(403, 'Stream identity mismatch.')
            call.stream_id = stream_id
        elif kind == 'hangup':
            if call.ended_at:
                return None
            call.ended_at = now()
            call.end_reason = (form.get('HangupCauseName') or form.get('CallStatus') or 'hangup')[:200]
            call.state = 'completed' if call.answered_at or form.get('AnswerTime') not in (None, '', 'NA') else 'failed'
            duration = form.get('BillDuration', '')
            if isinstance(duration, str) and duration.isdigit() and len(duration) < 10:
                call.bill_duration = int(duration)
            schedule_finalize(call)
            # Nobody answered, or the bridge never attached: drop the ringing session.
            if call.audio is not None and not call.audio.attached:
                from app.telephony.preroll import stop
                asyncio.create_task(stop(call), name=f'stop-preroll-{call.id}')
    return None


async def claim_stream(token: str) -> LiveCall | None:
    if not 30 <= len(token) <= 200:
        return None
    for call in list(_calls.values()):
        if not secrets.compare_digest(call.token, token):
            continue
        async with call.lock:
            if (call.used or call.ended_at or call.state != 'active' or call.cleanup_needed
                    or now() > call.created_at + timedelta(seconds=settings.MEDIA_GRANT_TTL_SECONDS)):
                return None
            call.used = True
            call.last_activity = now()
            return call
    return None


async def start_stream(attempt_id: UUID, stream_id: str):
    call = get(attempt_id)
    if not call:
        raise ValueError('Live call missing.')
    async with call.lock:
        if call.ended_at or call.state != 'active' or (call.stream_id and call.stream_id != stream_id):
            raise ValueError('Stream is no longer active.')
        call.stream_id = stream_id
        call.last_activity = now()


async def heartbeat(attempt_id: UUID) -> bool:
    call = get(attempt_id)
    if not call or call.ended_at or call.state != 'active':
        return False
    call.last_activity = now()
    return True


def schedule_finalize(call: LiveCall):
    if call.finalize_task is None or call.finalize_task.done():
        call.finalize_task = asyncio.create_task(finalize(call), name=f'finalize-{call.id}')


async def finalize(call: LiveCall):
    from app.auth.calls import persist_finished_call
    from app.gemini.summary import is_voicemail, summarize_call
    try:
        await asyncio.sleep(1)  # Let the Live session deliver its final transcript event.
        cause = (call.end_reason or 'no answer').replace('_', ' ')
        if not call.answered_at:
            # Never picked up (no answer, busy, rejected, switched off): retried later.
            call.summary = {'lead_status': 'NOT_ANSWERED', 'webinar_response': 'NO_RESPONSE',
                            'send_webinar_link': False, 'summary': f'Not answered ({cause}).'}
        elif is_voicemail(call):
            # Answered by voicemail / a carrier announcement: nobody reached, retried later.
            call.summary = {'lead_status': 'NO_RESPONSE', 'webinar_response': 'NO_RESPONSE',
                            'send_webinar_link': False, 'summary': 'Reached voicemail or an automated message.'}
        elif not any(speaker == 'customer' for speaker, _, _ in call.transcript):
            # Picked up but nobody spoke: treated like an unanswered call and retried.
            call.summary = {'lead_status': 'NO_RESPONSE', 'webinar_response': 'NO_RESPONSE',
                            'send_webinar_link': False, 'summary': f'Picked up but no response ({cause}).'}
        else:
            call.summary = await summarize_call(call)
        await persist_finished_call(call)
        _calls.pop(call.id, None)
    except Exception:
        logger.exception('Post-call finalization failed attempt=%s; will retry', call.id)


async def reconcile():
    """Provider-only recovery while a live call exists; never touches PostgreSQL."""
    from app.plivo import client
    for call in list(_calls.values()):
        if call.ended_at:
            if call.finalize_task is None or call.finalize_task.done():
                schedule_finalize(call)
            continue
        age = (now() - call.created_at).total_seconds()
        if age < settings.CALL_ANSWER_TIMEOUT_SECONDS + 20:
            continue
        call_id = call.provider_call_id or call.provider_request_id
        if call_id:
            try:
                record = await client.get_call(call_id)
            except client.ProviderError:
                record = None
            if record and record.get('end_time') not in (None, '', 'NA'):
                await callback(call.id, 'hangup', {
                    'CallUUID': call.provider_call_id or call_id, 'Direction': 'outbound',
                    'From': call.from_number, 'To': call.to_number,
                    'AnswerTime': record.get('answer_time') or '',
                    'HangupCauseName': record.get('hangup_cause_name', 'reconciled'),
                    'BillDuration': str(record.get('bill_duration', '')),
                })
                continue
        if age > settings.CALL_MAX_DURATION_SECONDS + settings.CALL_ANSWER_TIMEOUT_SECONDS + 30:
            call.state = 'failed'
            call.end_reason = 'provider_status_unknown'
            call.ended_at = now()
            schedule_finalize(call)
