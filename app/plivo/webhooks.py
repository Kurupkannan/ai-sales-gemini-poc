"""Outbound answer/control callbacks; no incoming-call application."""
from uuid import UUID
from xml.etree.ElementTree import Element, SubElement, tostring
from fastapi import APIRouter, HTTPException, Request, Response
from app.auth import calls
from app.config import settings
from app.plivo.client import callback_url
from app.plivo.security import signed_form
from app.telephony import runtime

router = APIRouter()


def answer_xml(attempt_id, token):
    response = Element('Response')
    if token:
        stream = SubElement(response, 'Stream', {
            'bidirectional': 'true', 'keepCallAlive': 'true', 'audioTrack': 'inbound',
            'contentType': 'audio/x-l16;rate=8000',
            'streamTimeout': str(settings.CALL_MAX_DURATION_SECONDS),
            'statusCallbackUrl': callback_url('stream-status', attempt_id), 'statusCallbackMethod': 'POST',
        })
        stream.text = settings.websocket_stream_url + '?token=' + token
    else:
        SubElement(response, 'Hangup')
    return tostring(response, encoding='utf-8', xml_declaration=True)


@router.post('/plivo/{kind}/{attempt_id}')
async def webhook(kind: str, attempt_id: UUID, request: Request):
    if kind not in {'answer', 'ring', 'hangup', 'stream-status'}:
        raise HTTPException(404)
    form = await signed_form(request)
    token = await runtime.callback(attempt_id, kind, form)
    if kind == 'answer':
        return Response(answer_xml(attempt_id, token), media_type='application/xml', headers={'Cache-Control': 'no-store'})
    return Response(status_code=200)
