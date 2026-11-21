"""Outcomes stay correct when callers reach voicemail or the summary service is down."""
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.gemini.agent_prompt import NO_REPLY, YES_REPLY
from app.gemini.live_session import build_live_connect_config
from app.gemini.summary import fallback_summary, is_voicemail


def _call(*lines):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(id=uuid4(), transcript=[(speaker, text, now) for speaker, text in lines])


def test_voicemail_and_carrier_announcements_are_detected():
    assert is_voicemail(_call(('customer', 'Your call has been forwarded to voicemail. Please record your message')))
    assert is_voicemail(_call(('customer', 'The number you are calling is switched off')))
    assert not is_voicemail(_call(('customer', 'ശരി, വരാം')))


def test_summary_fallback_reads_the_closing_line():
    assert fallback_summary(_call(('customer', 'ok'), ('agent', YES_REPLY)), 'x')['webinar_response'] == 'YES'
    no = fallback_summary(_call(('customer', 'ഇല്ല'), ('agent', NO_REPLY)), 'x')
    assert no['webinar_response'] == 'NO' and no['send_webinar_link'] is True
    assert fallback_summary(_call(('customer', 'hmm')), 'x')['lead_status'] == 'UNKNOWN'


def test_speech_recognition_expects_malayalam_english_and_hindi():
    codes = build_live_connect_config('x').input_audio_transcription.language_codes
    assert codes[0] == 'ml-IN' and {'en-IN', 'hi-IN'} <= set(codes)
