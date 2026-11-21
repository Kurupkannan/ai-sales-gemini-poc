"""Validate the externally visible Plivo request before touching call data."""
from urllib.parse import parse_qsl
import hmac
import hashlib
import base64
from fastapi import HTTPException
from plivo.utils import validate_v3_signature
from plivo.utils.signature_v3 import construct_get_url, get_signature_v3
from app.config import settings

MAX_CALLBACK_BYTES = 16384


def valid_signature(connection, params: dict | None = None) -> bool:
    signature = connection.headers.get('x-plivo-signature-v3', '')
    nonce = connection.headers.get('x-plivo-signature-v3-nonce', '')
    secret = settings.PLIVO_AUTH_TOKEN.get_secret_value()
    if not signature or not nonce or not secret or len(signature) > 1024 or len(nonce) > 256:
        return False
    try:
        # Canonical public URL, independent of proxy/internal host. Plivo signs
        # the WSS stream URL; the SDK's URL validator only accepts HTTP(S), so
        # use its V3 signing primitives for WebSocket upgrades.
        raw_path = connection.scope.get('raw_path', connection.scope['path'].encode()).decode('ascii')
        query = connection.scope.get('query_string', b'').decode('ascii')
        suffix = raw_path + ('?' + query if query else '')
        if connection.scope['type'] == 'websocket':
            urls = (settings.public_origin.replace('https://', 'wss://', 1) + suffix,
                    settings.public_origin + suffix)
            candidates = signature.split(',')
            for url in urls:
                base = construct_get_url(url, {}).decode('utf-8')
                expected = get_signature_v3(secret.encode(), base, nonce.encode()).decode()
                if any(hmac.compare_digest(expected, candidate.strip()) for candidate in candidates):
                    return True
                # Plivo's audio-stream protocol also documents the upgrade
                # signature as HMAC-SHA256 over GET + full URI + nonce.
                stream_expected = base64.b64encode(hmac.new(secret.encode(),
                    f'GET{url}{nonce}'.encode(), hashlib.sha256).digest()).decode()
                if any(hmac.compare_digest(stream_expected, candidate.strip()) for candidate in candidates):
                    return True
            return False
        return validate_v3_signature(connection.method, settings.public_origin + suffix,
            nonce, secret, signature, params or {})
    except Exception:
        return False


async def signed_form(request) -> dict[str, str]:
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/x-www-form-urlencoded':
        raise HTTPException(415, 'Expected form callback.')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_CALLBACK_BYTES:
            raise HTTPException(413, 'Callback too large.')
    try:
        pairs = parse_qsl(body.decode('utf-8'), keep_blank_values=True, max_num_fields=100)
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid callback.') from None
    params = dict(pairs)
    if len(params) != len(pairs) or not valid_signature(request, params):
        raise HTTPException(403, 'Invalid provider signature.')
    return params
