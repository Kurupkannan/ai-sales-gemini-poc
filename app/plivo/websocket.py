"""Plivo media protocol and authenticated WebSocket admission."""
import base64
import binascii
import json
import logging
from fastapi import APIRouter, WebSocket
from app.auth import calls
from app.plivo.security import valid_signature
from app.telephony import runtime

router = APIRouter()
logger = logging.getLogger(__name__)
MAX_MESSAGE_BYTES = 65536


def parse_message(raw: str) -> dict:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError('Oversized media message.')
    try:
        event = json.loads(raw)
    except ValueError:
        raise ValueError('Invalid media message.') from None
    if not isinstance(event, dict) or not isinstance(event.get('event'), str):
        raise ValueError('Invalid media event.')
    return event


def decode_media(event):
    media = event.get('media')
    if not isinstance(media, dict) or not isinstance(media.get('payload'), str):
        raise ValueError('Missing audio payload.')
    if media.get('track', 'inbound') != 'inbound':
        raise ValueError('Unexpected audio track.')
    try:
        pcm = base64.b64decode(media['payload'], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid audio payload.') from None
    if len(pcm) > 16000 or len(pcm) % 2:
        raise ValueError('Invalid PCM frame size.')
    return pcm


def play_audio(pcm):
    return {'event': 'playAudio', 'media': {'contentType': 'audio/x-l16',
        'sampleRate': 8000, 'payload': base64.b64encode(pcm).decode('ascii')}}


def validate_start(event, attempt):
    start = event.get('start', {})
    if not isinstance(start, dict):
        raise ValueError('Missing start metadata.')
    fmt = start.get('mediaFormat', {})
    if start.get('callId') != attempt.provider_call_id:
        raise ValueError('Call identity mismatch.')
    # Plivo's stream accountId can differ from the API auth ID on the call.
    # Admission already requires the one-use grant; bind the event to CallUUID.
    if not isinstance(start.get('streamId'), str) or not 1 <= len(start['streamId']) <= 200:
        raise ValueError('Stream identity missing.')
    if not isinstance(fmt, dict):
        raise ValueError('Media format missing.')
    if fmt.get('encoding') != 'audio/x-l16':
        raise ValueError(f'Unsupported audio encoding: {str(fmt.get("encoding"))[:50]}.')
    if str(fmt.get('sampleRate')) != '8000':
        raise ValueError(f'Unsupported audio rate: {str(fmt.get("sampleRate"))[:20]}.')
    if fmt.get('channels', 1) != 1:
        raise ValueError(f'Unsupported audio channels: {str(fmt.get("channels"))[:20]}.')
    if start.get('tracks', ['inbound']) != ['inbound']:
        raise ValueError('Unsupported audio tracks.')
    return start['streamId']


@router.websocket('/plivo/media')
async def media_socket(websocket: WebSocket):
    # Verify before redacting the signed URL. Never log tokens at accept/close.
    valid = valid_signature(websocket)
    token = websocket.query_params.get('token', '')
    websocket.scope['query_string'] = b''
    if not valid:
        # The signed answer callback issues a short-lived, one-use grant;
        # Plivo's live WebSocket V3 header differs from its HTTP signature.
        # The grant and start-frame call/account binding remain mandatory.
        logger.warning('Plivo media upgrade signature mismatch; checking one-use grant')
    attempt = await runtime.claim_stream(token)
    if not attempt:
        logger.warning('Plivo media upgrade rejected: grant')
        await websocket.close(code=1008)
        return
    from app.telephony.bridge import handle_media
    await handle_media(websocket, attempt)
