"""Plivo-only orchestration; DB updates happen outside provider I/O."""
import asyncio
import logging
from fastapi import HTTPException
from app.config import settings
from app.auth import calls
from app.plivo import client
from app.telephony import runtime
from app.telephony.preroll import begin, stop, wait_for_greeting
from app.timing import measure, call_mark

logger = logging.getLogger(__name__)
_dial_lock = asyncio.Lock()
_last_dial = 0.0


async def pace_dial():
    """Space provider dials by TELEPHONY_DIALS_PER_SECOND; concurrent callers queue here."""
    global _last_dial
    loop = asyncio.get_running_loop()
    async with _dial_lock:
        delay = _last_dial + 1 / settings.TELEPHONY_DIALS_PER_SECOND - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        _last_dial = loop.time()


async def initiate(org_id, identity, payload):
    if not settings.OUTBOUND_CALLS_ENABLED:
        raise HTTPException(503, 'Outbound calling is disabled.')
    with measure("authorize_call"):
        attempt = await calls.authorize_call(org_id, identity, payload)
    live = runtime.register(attempt)
    call_mark(live, "prepare_start")
    # Authorization is committed. Prepare actual audio before ringing the phone.
    begin(live)
    try:
        with measure('greeting_prepare'):
            await wait_for_greeting(live)
    except (Exception, asyncio.CancelledError) as exc:
        logger.warning('Greeting preparation failed attempt=%s type=%s', live.id, type(exc).__name__)
        live.state = 'failed'
        live.ended_at = runtime.now()
        live.end_reason = 'greeting_unavailable'
        await stop(live)
        runtime.schedule_finalize(live)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return live.snapshot()
    await pace_dial()
    call_mark(live, 'dial_start')
    try:
        request_id = await client.create_call(attempt)
    except client.ProviderError as exc:
        state = 'unknown' if exc.uncertain else 'failed'
        logger.warning('Dial result attempt=%s state=%s provider_status=%s', attempt.id, state, exc.status_code)
        live.state = state
        if state == 'failed':
            live.ended_at = runtime.now()
            live.end_reason = 'dial_rejected'
            await stop(live)
            runtime.schedule_finalize(live)
        return live.snapshot()
    call_mark(live, "dial_response")
    live.provider_request_id = request_id
    if live.state == 'pending':
        live.state = 'initiated'
    return live.snapshot()


async def terminate(ident, reason='bridge_closed'):
    live = runtime.get(ident)
    if live:
        if live.ended_at:
            return
        live.cleanup_needed = True
        live.end_reason = reason
        try:
            if live.provider_call_id:
                await client.hangup(live.provider_call_id)
            elif live.provider_request_id:
                await client.cancel_request(live.provider_request_id)
        except client.ProviderError:
            logger.warning('Call cleanup pending attempt=%s', ident)
        return
    row = await calls.request_cleanup(ident, reason)
    if row is None:
        return
    try:
        if row.provider_call_id:
            await client.hangup(row.provider_call_id)
        elif row.provider_request_id:
            await client.cancel_request(row.provider_request_id)
        else:
            # No evidence of rejection: keep reserved until a callback/operator resolves it.
            return
        await calls.cleanup_complete(ident)
    except client.ProviderError:
        logger.warning('Call cleanup pending attempt=%s', ident)
