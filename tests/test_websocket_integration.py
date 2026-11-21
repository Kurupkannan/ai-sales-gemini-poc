"""Exercise the complete audio bridge with asynchronous peers and no network."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from app.telephony import bridge, preroll
from app.audio.converter import pcm_to_base64


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = False
    async def accept(self):
        pass
    async def close(self):
        self.closed = True
    async def receive_text(self):
        return json.dumps(await self.incoming.get())
    async def send_json(self, value):
        await self.outgoing.put(value)


class Session:
    def __init__(self):
        self.responses = asyncio.Queue()
        self.send_realtime_input = AsyncMock()
        self.send_client_content = AsyncMock()
    def receive(self):
        async def gen():
            message = await self.responses.get()
            if isinstance(message, Exception):
                raise message
            yield message
        return gen()


def response(pcm=None, interrupted=False):
    parts = [] if pcm is None else [SimpleNamespace(inline_data=SimpleNamespace(data=pcm))]
    return SimpleNamespace(server_content=SimpleNamespace(interrupted=interrupted,
        model_turn=SimpleNamespace(parts=parts), turn_complete=not interrupted))


@pytest.fixture
async def running(monkeypatch):
    socket, session = Socket(), Session()
    ended = asyncio.Event()
    @asynccontextmanager
    async def connect(self):
        try:
            yield session
        finally:
            ended.set()
    monkeypatch.setattr(preroll.GeminiLiveManager, 'connect', connect)
    monkeypatch.setattr(bridge.runtime, 'start_stream', AsyncMock())
    monkeypatch.setattr(bridge.runtime, 'heartbeat', AsyncMock(return_value=True))
    terminate = AsyncMock()
    monkeypatch.setattr(bridge, 'terminate', terminate)
    attempt = SimpleNamespace(id=uuid4(), provider_account_id='MA123', provider_call_id='call-1')
    await socket.incoming.put({'event':'start','start':{'callId':'call-1','accountId':'MA123','streamId':'stream-1',
        'mediaFormat':{'encoding':'audio/x-l16','sampleRate':8000}}})
    task = asyncio.create_task(bridge.handle_media(socket, attempt))
    yield socket, session, task, ended, terminate
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def next_audio(socket):
    async with asyncio.timeout(2):
        while True:
            event = await socket.outgoing.get()
            if event['event'] == 'playAudio':
                return event


async def test_multi_turn_and_inbound_audio(running):
    socket, session, task, ended, terminate = running
    for index in range(3):
        await session.responses.put(response(b'\x10\x00'*480))
        assert (await next_audio(socket))['media']['sampleRate'] == 8000
        await socket.incoming.put({'event':'media','media':{'payload':pcm_to_base64(b'\x01\x00'*160)}})
    for _ in range(100):
        await socket.incoming.put({'event':'media','media':{'payload':pcm_to_base64(b'\x01\x00'*160)}})
    await socket.incoming.put({'event':'stop'})
    await asyncio.wait_for(task, 2)
    assert session.send_realtime_input.await_count == 103
    session.send_client_content.assert_awaited_once()
    assert ended.is_set() and socket.closed
    terminate.assert_awaited_once()


async def test_long_greeting_waits_for_phone_playback(running):
    socket, session, task, _, _ = running
    await session.responses.put(response(b'\x10\x00' * (24000 * 6)))
    assert (await next_audio(socket))['event'] == 'playAudio'
    await asyncio.sleep(0.1)
    assert not task.done()
    await socket.incoming.put({'event': 'stop'})
    await asyncio.wait_for(task, 2)


async def test_interruption_clears_buffer_and_drops_old_audio(running):
    socket, session, task, ended, terminate = running
    await session.responses.put(response(b'\x00\x20' * 24000))  # one second buffered speech
    await next_audio(socket)
    await session.responses.put(response(b'\xff\x7f'*480, interrupted=True))
    async with asyncio.timeout(2):
        events = []
        while True:
            event = await socket.outgoing.get()
            events.append(event)
            if event['event'] == 'clearAudio':
                break
    assert len(events) <= 3
    await asyncio.sleep(.05)
    assert socket.outgoing.empty()
    await session.responses.put(response(b'\x00\x01'*480))
    assert (await next_audio(socket))['event'] == 'playAudio'
    await socket.incoming.put({'event':'stop'})
    await asyncio.wait_for(task, 2)


async def test_gemini_failure_closes_session_and_call(running):
    socket, session, task, ended, terminate = running
    await session.responses.put(RuntimeError('test provider failure'))
    await asyncio.wait_for(task, 2)
    assert ended.is_set() and socket.closed
    assert terminate.call_args.args[1] == 'bridge_failure'


async def test_greeting_is_buffered_before_pickup(monkeypatch):
    """Audio generated while ringing is the first thing played after answer."""
    socket, session = Socket(), Session()
    connects = 0

    @asynccontextmanager
    async def connect(self):
        nonlocal connects
        connects += 1
        try:
            yield session
        finally:
            pass

    monkeypatch.setattr(preroll.GeminiLiveManager, 'connect', connect)
    monkeypatch.setattr(bridge.runtime, 'start_stream', AsyncMock())
    monkeypatch.setattr(bridge.runtime, 'heartbeat', AsyncMock(return_value=True))
    monkeypatch.setattr(bridge, 'terminate', AsyncMock())
    attempt = SimpleNamespace(id=uuid4(), provider_call_id='call-1', lead_name='Asha')
    audio = preroll.begin(attempt)
    await session.responses.put(response(b'\x10\x00' * 480))
    async with asyncio.timeout(2):
        while not audio.early:
            await asyncio.sleep(0.01)
    buffered = len(audio.early)
    await socket.incoming.put({'event': 'start', 'start': {
        'callId': 'call-1', 'streamId': 'stream-1',
        'mediaFormat': {'encoding': 'audio/x-l16', 'sampleRate': 8000}}})
    task = asyncio.create_task(bridge.handle_media(socket, attempt))
    try:
        assert (await next_audio(socket))['event'] == 'playAudio'
        assert connects == 1 and buffered >= 1 and audio.attached
        await socket.incoming.put({'event': 'stop'})
        await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_wrong_stream_rejected(running):
    socket, session, task, ended, terminate = running
    await socket.incoming.put({'event':'media','streamId':'another-call','media':{'payload':'AA=='}})
    await asyncio.wait_for(task, 2)
    assert session.send_realtime_input.await_count == 0
    assert terminate.call_args.args[1] == 'bridge_failure'
