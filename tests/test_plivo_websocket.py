import base64
import json
from types import SimpleNamespace
import pytest
from app.plivo.websocket import parse_message, decode_media, play_audio, validate_start


def test_media_contract():
    pcm = b'\x00\x10' * 160
    event = play_audio(pcm)
    assert event['event'] == 'playAudio'
    assert event['media']['contentType'] == 'audio/x-l16'
    assert event['media']['sampleRate'] == 8000
    assert decode_media({'media': event['media']}) == pcm


@pytest.mark.parametrize('raw', ['not-json', '[]', '{}', '{"event":2}', 'x'*65537])
def test_bad_message(raw):
    with pytest.raises(ValueError):
        parse_message(raw)


@pytest.mark.parametrize('payload', ['%%%%', base64.b64encode(b'x').decode(), base64.b64encode(b'x'*16002).decode()])
def test_invalid_pcm(payload):
    with pytest.raises(ValueError):
        decode_media({'media': {'payload': payload}})


def test_start_requires_call_and_format():
    attempt = SimpleNamespace(provider_account_id='MA123', provider_call_id='call')
    event = {'start': {'accountId':'MA123', 'callId':'call', 'streamId':'stream',
        'mediaFormat':{'encoding':'audio/x-l16','sampleRate':8000}}}
    assert validate_start(event, attempt) == 'stream'
    event['start']['accountId'] = 'different-stream-account'
    assert validate_start(event, attempt) == 'stream'
    for field, value in [('callId','wrong'), ('mediaFormat',{'encoding':'audio/x-mulaw','sampleRate':8000})]:
        bad = json.loads(json.dumps(event))
        bad['start'][field] = value
        with pytest.raises(ValueError):
            validate_start(bad, attempt)


def test_signed_upgrade_uses_public_url_then_redacts_query(plivo_config, monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from plivo.utils.signature_v3 import construct_get_url, get_signature_v3
    from app.main import app
    from app.plivo import websocket as module
    from app.telephony import bridge
    token = 'a'*43
    path = '/plivo/media?token=' + token
    base = construct_get_url('wss://voice.example.com' + path, {}).decode()
    signature = get_signature_v3(b'unit-test-token', base, b'nonce').decode()
    claim = AsyncMock(return_value=object())
    monkeypatch.setattr(module.runtime, 'claim_stream', claim)
    async def handle(socket, attempt):
        assert socket.scope['query_string'] == b''
        await socket.accept()
        await socket.send_json({'ok': True})
        await socket.close()
    monkeypatch.setattr(bridge, 'handle_media', handle)
    with TestClient(app).websocket_connect(path, headers={
        'X-Plivo-Signature-V3': signature, 'X-Plivo-Signature-V3-Nonce':'nonce'}) as ws:
        assert ws.receive_json() == {'ok': True}
    claim.assert_awaited_once_with(token)


def test_https_upgrade_signature_is_also_accepted(plivo_config, monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from plivo.utils.signature_v3 import construct_get_url, get_signature_v3
    from app.main import app
    from app.plivo import websocket as module
    token = 'a'*43
    path = '/plivo/media?token=' + token
    base = construct_get_url('https://voice.example.com' + path, {}).decode()
    signature = get_signature_v3(b'unit-test-token', base, b'nonce').decode()
    claim = AsyncMock(return_value=object())
    monkeypatch.setattr(module.runtime, 'claim_stream', claim)
    from app.telephony import bridge
    async def handle(socket, attempt):
        await socket.accept()
        await socket.close()
    monkeypatch.setattr(bridge, 'handle_media', handle)
    with TestClient(app).websocket_connect(path, headers={
        'X-Plivo-Signature-V3': signature, 'X-Plivo-Signature-V3-Nonce': 'nonce'}):
        pass
    claim.assert_awaited_once_with(token)


def test_documented_stream_signature_is_accepted(plivo_config, monkeypatch):
    import hashlib
    import hmac
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from app.main import app
    from app.plivo import websocket as module
    from app.telephony import bridge
    token = 'a'*43
    path = '/plivo/media?token=' + token
    signed = hmac.new(b'unit-test-token', ('GETwss://voice.example.com' + path + 'nonce').encode(), hashlib.sha256).digest()
    signature = base64.b64encode(signed).decode()
    claim = AsyncMock(return_value=object())
    monkeypatch.setattr(module.runtime, 'claim_stream', claim)
    async def handle(socket, attempt):
        await socket.accept()
        await socket.close()
    monkeypatch.setattr(bridge, 'handle_media', handle)
    with TestClient(app).websocket_connect(path, headers={
        'X-Plivo-Signature-V3': signature, 'X-Plivo-Signature-V3-Nonce': 'nonce'}):
        pass
    claim.assert_awaited_once_with(token)


def test_bad_upgrade_still_requires_valid_grant(plivo_config, monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from app.main import app
    from app.plivo import websocket as module
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(module.runtime, 'claim_stream', claim)
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect('/plivo/media?token='+'a'*43,
            headers={'X-Plivo-Signature-V3':'forged','X-Plivo-Signature-V3-Nonce':'nonce'}):
            pass
    claim.assert_awaited_once_with('a'*43)


def test_retired_media_route_absent():
    from app.main import app
    from fastapi.routing import APIWebSocketRoute
    paths = [route.path for outer in app.routes
             for route in getattr(getattr(outer, 'original_router', None), 'routes', [outer])
             if isinstance(route, APIWebSocketRoute)]
    assert paths == ['/plivo/media']
