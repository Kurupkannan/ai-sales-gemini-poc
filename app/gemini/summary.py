"""Summarize a completed voice screening call from its Live transcripts."""
import json
import logging
import re
from typing import Literal
from pydantic import BaseModel, Field, StrictBool, model_validator

from google import genai
from google.genai import types

from app.config import settings
from app.gemini.agent_prompt import LEAD_CLASSIFICATION_PROMPT

logger = logging.getLogger(__name__)
STATUSES = {'HOT', 'WARM', 'COLD', 'NOT_INTERESTED', 'CALLBACK', 'WRONG_NUMBER', 'DO_NOT_CALL', 'NOT_GENUINE', 'UNKNOWN'}
# Carrier voicemail / unreachable announcements (English, Malayalam, Hindi).
VOICEMAIL = re.compile(r'voice ?mail|record your message|after the tone|not available|not reachable|switched off|'
                       r'forwarded|ലഭ്യമല്ല|സ്വിച്ച് ഓഫ്|परिधि से बाहर|उपलब्ध नहीं|स्विच ऑफ', re.I)


def is_voicemail(call) -> bool:
    return any(speaker == 'customer' and VOICEMAIL.search(text) for speaker, text, _ in call.transcript)


def fallback_summary(call, why: str) -> dict:
    """When the summary model is unavailable, read the outcome from the agent's own closing line."""
    from app.gemini.agent_prompt import NO_REPLY, YES_REPLY
    agent = ' '.join(text for speaker, text, _ in call.transcript if speaker == 'agent')
    if YES_REPLY[:18] in agent:
        return {'lead_status': 'WARM', 'webinar_response': 'YES', 'send_webinar_link': True,
                'summary': f'Agreed to join the webinar (closing line; {why}).'}
    if NO_REPLY[:18] in agent:
        return {'lead_status': 'COLD', 'webinar_response': 'NO', 'send_webinar_link': True,
                'summary': f'Did not commit to joining; link promised (closing line; {why}).'}
    return {'lead_status': 'UNKNOWN', 'summary': f'Automatic summary unavailable ({why}); review the transcript.'}


class ScreeningSummary(BaseModel):
    lead_status: Literal['HOT', 'WARM', 'COLD', 'NOT_INTERESTED', 'CALLBACK', 'WRONG_NUMBER', 'DO_NOT_CALL', 'NOT_GENUINE', 'UNKNOWN']
    genuineness: Literal['CONFIRMED', 'DENIED', 'UNCONFIRMED'] = 'UNCONFIRMED'
    genuineness_evidence: str | None = None
    explicitly_not_interested: StrictBool = False
    currently_trading: StrictBool | None = None
    experience: str | None = None
    markets: list[str] = Field(default_factory=list)
    wants_course: StrictBool | None = None
    start_timeline: Literal['immediately', 'within 1-3 months', 'later', 'unknown'] = 'unknown'
    ok_with_paid_course: StrictBool | None = None
    preferred_callback_time: str | None = None
    main_problem: str | None = None
    # Webinar invitation outcome; the team sends the Zoom link to everyone flagged here.
    webinar_response: Literal['YES', 'NO', 'BUSY', 'NO_RESPONSE', 'UNKNOWN'] = 'UNKNOWN'
    send_webinar_link: StrictBool = False
    summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def evidence_required(self):
        if not self.summary.strip():
            raise ValueError('Missing summary')
        if self.explicitly_not_interested and self.lead_status not in {
            'DO_NOT_CALL', 'WRONG_NUMBER', 'NOT_GENUINE'
        }:
            self.lead_status = 'NOT_INTERESTED'
            self.wants_course = False
        if self.lead_status == 'HOT' and not (
            self.genuineness == 'CONFIRMED' and self.wants_course is True
            and self.start_timeline == 'immediately' and self.ok_with_paid_course is True
        ):
            self.lead_status = 'WARM' if self.wants_course is True else 'UNKNOWN'
        if self.lead_status == 'NOT_GENUINE' and self.genuineness != 'DENIED':
            self.lead_status = 'UNKNOWN'
        return self


async def summarize_call(call) -> dict:
    transcript = '\n'.join(f'{speaker}: {text}' for speaker, text, _ in call.transcript)
    client = None
    try:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        response = await client.aio.models.generate_content(
            model=settings.GEMINI_SUMMARY_MODEL,
            contents=f'{LEAD_CLASSIFICATION_PROMPT}\n\nCall transcript:\n{transcript[:30000]}',
            config=types.GenerateContentConfig(response_mime_type='application/json', response_schema=ScreeningSummary),
        )
        value = json.loads(response.text or '{}')
        return ScreeningSummary.model_validate(value).model_dump()
    except Exception:
        logger.exception('Gemini post-call summary unavailable attempt=%s', call.id)
        return fallback_summary(call, 'summary service unavailable')
    finally:
        if client:
            await client.aio.aclose()
