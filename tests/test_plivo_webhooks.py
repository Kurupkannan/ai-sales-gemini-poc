from uuid import uuid4
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET
import pytest
from httpx import AsyncClient, ASGITransport
from plivo.utils.signature_v3 import construct_post_url, get_signature_v3
from app.main import app
from app.plivo import webhooks
from app.plivo.security import valid_signature
from starlette.requests import Request


def headers(url, form, token='unit-test-token'):
    nonce = 'test-nonce'
    base = construct_post_url(url, form)
    signature = get_signature_v3(token.encode(), base.decode(), nonce.encode()).decode()
    return {'X-Plivo-Signature-V3': signature, 'X-Plivo-Signature-V3-Nonce': nonce}


async def test_signed_answer_and_duplicate(plivo_config, monkeypatch):
    ident = uuid4()
    path = f'/plivo/answer/{ident}'
    form = {'CallUUID': str(uuid4()), 'Direction': 'outbound', 'From': '919876543211', 'To': '919876543210'}
    callback = AsyncMock(return_value='a'*43)
    monkeypatch.setattr(webhooks.runtime, 'callback', callback)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://internal') as http:
        responses = [await http.post(path, data=form, headers=headers('https://voice.example.com'+path, form)) for _ in range(2)]
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].content == responses[1].content
    root = ET.fromstring(responses[0].content)
    stream = root.find('Stream')
    assert stream.attrib['bidirectional'] == 'true'
    assert stream.attrib['audioTrack'] == 'inbound'
    assert stream.attrib['contentType'] == 'audio/x-l16;rate=8000'
    assert stream.text == 'wss://voice.example.com/plivo/media?token=' + 'a'*43
    assert root.find('Hangup') is None
    assert responses[0].headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('change', ['body', 'url', 'secret', 'missing'])
async def test_signature_rejects_tampering(plivo_config, monkeypatch, change):
    path = f'/plivo/answer/{uuid4()}'
    form = {'CallUUID': str(uuid4()), 'Direction': 'outbound'}
    signed_url = 'https://voice.example.com' + path
    h = headers(signed_url + ('?bad=1' if change == 'url' else ''), form,
        token='other' if change == 'secret' else 'unit-test-token')
    if change == 'body':
        form['Direction'] = 'inbound'
    if change == 'missing':
        h = {}
    callback = AsyncMock()
    monkeypatch.setattr(webhooks.runtime, 'callback', callback)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
        response = await http.post(path, data=form, headers=h)
    assert response.status_code == 403
    callback.assert_not_called()


async def test_bounded_callback_and_duplicate_keys(plivo_config, monkeypatch):
    callback = AsyncMock()
    monkeypatch.setattr(webhooks.runtime, 'callback', callback)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
        for body, expected in [(b'a=' + b'x'*17000, 413), (b'a=1&a=2', 403)]:
            response = await http.post(f'/plivo/answer/{uuid4()}', content=body,
                headers={'Content-Type': 'application/x-www-form-urlencoded'})
            assert response.status_code == expected
    callback.assert_not_called()


def test_rejected_answer_ends_call():
    root = ET.fromstring(webhooks.answer_xml(uuid4(), None))
    assert root.find('Stream') is None
    assert root.find('Hangup') is not None


@pytest.mark.parametrize('url', ['http://voice.example.com', 'https://user:pass@voice.example.com',
    'https://voice.example.com/prefix', 'https://voice.example.com?x=1', ''])
def test_public_origin_fails_closed(plivo_config, monkeypatch, url):
    monkeypatch.setattr(plivo_config, 'PUBLIC_BASE_URL', url)
    with pytest.raises(ValueError):
        plivo_config.validate_plivo()
