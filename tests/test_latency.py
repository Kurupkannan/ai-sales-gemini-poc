import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.timing import RequestTiming, measure, current, call_mark
from app.telephony import service


async def test_request_timing_isolated_and_redacted(caplog):
    async def app(scope, receive, send):
        with measure('auth'):
            await asyncio.sleep(0.001)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok'})
    with caplog.at_level('INFO', logger='app.timing'):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=RequestTiming(app)), base_url='http://test') as client:
            responses = await asyncio.gather(*(client.get('/private?token=secret') for _ in range(2)))
    assert responses[0].headers['x-request-id'] != responses[1].headers['x-request-id']
    assert all('auth;dur=' in response.headers['server-timing'] for response in responses)
    assert 'secret' not in caplog.text and '/private' not in caplog.text
    assert current.get() is None


@pytest.mark.parametrize('uncertain', [True, False])
async def test_preroll_precedes_dial_and_rejection_cleans_up(plivo_config, monkeypatch, uncertain):
    from app.plivo.client import ProviderError
    started = asyncio.Event()
    row = SimpleNamespace(id=uuid4())
    live = SimpleNamespace(id=row.id, state='pending', snapshot=lambda: {'status': live.state})
    monkeypatch.setattr(service.calls, 'authorize_call', AsyncMock(return_value=row))
    monkeypatch.setattr(service.runtime, 'register', lambda _: live)
    monkeypatch.setattr(service, 'begin', lambda _: started.set())
    monkeypatch.setattr(service, 'wait_for_greeting', AsyncMock())
    stop = AsyncMock()
    finalize = []
    monkeypatch.setattr(service, 'stop', stop)
    monkeypatch.setattr(service.runtime, 'schedule_finalize', finalize.append)
    async def dial(attempt):
        assert started.is_set(), 'Greeting must begin before waiting for the provider'
        raise ProviderError(uncertain=uncertain)
    monkeypatch.setattr(service.client, 'create_call', dial)
    await service.initiate(uuid4(), object(), object())
    assert live.state == ('unknown' if uncertain else 'failed')
    assert stop.await_count == (0 if uncertain else 1)
    assert len(finalize) == (0 if uncertain else 1)


def test_call_timings_are_once_only_and_not_in_public_snapshot(caplog):
    call = SimpleNamespace(id=uuid4())
    with caplog.at_level('INFO', logger='app.timing'):
        call_mark(call, 'answer')
        call_mark(call, 'first_audio_sent')
        call_mark(call, 'first_audio_sent')
    assert caplog.text.count('stage=first_audio_sent') == 1
    assert 'answer_elapsed_ms=' in caplog.text


@pytest.mark.parametrize('failure', [None, TimeoutError(), RuntimeError('unavailable')])
async def test_no_dial_until_greeting_ready(plivo_config, monkeypatch, failure):
    entered, release = asyncio.Event(), asyncio.Event()
    row = SimpleNamespace(id=uuid4())
    live = SimpleNamespace(id=row.id, state='pending', snapshot=lambda: {'status': live.state})
    monkeypatch.setattr(service.calls, 'authorize_call', AsyncMock(return_value=row))
    monkeypatch.setattr(service.runtime, 'register', lambda _: live)
    monkeypatch.setattr(service, 'begin', lambda _: None)
    async def prepare(call):
        entered.set()
        await release.wait()
        if failure:
            raise failure
    monkeypatch.setattr(service, 'wait_for_greeting', prepare)
    stop, finalize = AsyncMock(), []
    monkeypatch.setattr(service, 'stop', stop)
    monkeypatch.setattr(service.runtime, 'schedule_finalize', finalize.append)
    dial = AsyncMock(return_value=str(uuid4()))
    monkeypatch.setattr(service.client, 'create_call', dial)
    task = asyncio.create_task(service.initiate(uuid4(), object(), object()))
    await asyncio.wait_for(entered.wait(), 1)
    dial.assert_not_awaited()
    release.set()
    await asyncio.wait_for(task, 1)
    if failure:
        dial.assert_not_awaited()
        assert live.state == 'failed' and live.end_reason == 'greeting_unavailable'
        stop.assert_awaited_once_with(live)
        assert finalize == [live]
    else:
        dial.assert_awaited_once_with(row)
        assert live.state == 'initiated'


async def test_greeting_readiness_requires_audio_not_just_connected_session(monkeypatch):
    from app.telephony import preroll
    audio = preroll.PreparedAudio()
    audio.ready.set()
    audio._task = asyncio.create_task(asyncio.sleep(60))
    call = SimpleNamespace(id=uuid4(), audio=audio)
    monkeypatch.setattr(preroll.settings, 'INITIAL_GREETING', 'Hello')
    monkeypatch.setattr(preroll.settings, 'CALL_GREETING_PREPARE_TIMEOUT_SECONDS', 0.02)
    try:
        with pytest.raises(TimeoutError):
            await preroll.wait_for_greeting(call)
        audio.early.append((0, {'event': 'playAudio'}))
        audio.first_audio.set()
        await preroll.wait_for_greeting(call)
        assert not audio._task.done()
    finally:
        await preroll.stop(call, force=True)
