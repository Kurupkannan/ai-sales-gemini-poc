from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import json
import pytest
from app.gemini.agent_prompt import short_name, greeting_for, INITIAL_GREETING
from app.gemini.summary import ScreeningSummary, summarize_call


@pytest.mark.parametrize('full,short', [
    ('Joel Sunny Varghese', 'Joel'), ('Dr. K. R. Rahul Nair', 'Rahul'),
    ('K.R. Anjali Menon', 'Anjali'), ('Pinarayi, Vijayan', 'Vijayan'),
    ('അഞ്ജലി മേനോൻ', 'അഞ്ജലി'), ('Muhammed Shafi', 'Muhammed'),
    ('A. K.', ''), (None, ''), ('', ''), ('Ms Asha Thomas', 'Asha'),
])
def test_address_name(full, short):
    assert short_name(full) == short


def test_named_greeting_asks_one_question_and_omits_family_names():
    greeting = greeting_for('Joel Sunny Varghese', INITIAL_GREETING)
    assert 'Joel' in greeting and 'Sunny' not in greeting and 'Varghese' not in greeting
    assert greeting.count('?') == 1


def test_hot_requires_explicit_evidence_and_fees_not_silence():
    fields = dict(lead_status='HOT', genuineness='CONFIRMED', wants_course=True,
                  start_timeline='immediately', summary='Interested in learning soon.')
    assert ScreeningSummary(**fields).lead_status == 'WARM'
    assert ScreeningSummary(**fields, ok_with_paid_course=True).lead_status == 'HOT'
    assert ScreeningSummary(lead_status='NOT_GENUINE', summary='Call interrupted.').lead_status == 'UNKNOWN'


async def test_incomplete_callback_keeps_time_without_inventing_interest(monkeypatch):
    from app.gemini import summary
    data = {'lead_status': 'CALLBACK', 'genuineness': 'UNCONFIRMED',
            'preferred_callback_time': 'tomorrow', 'summary': 'Requested a callback tomorrow; screening incomplete.'}
    generate = AsyncMock(return_value=SimpleNamespace(text=json.dumps(data)))
    close = AsyncMock()
    monkeypatch.setattr(summary.genai, 'Client', lambda **kw: SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate), aclose=close)))
    result = await summarize_call(SimpleNamespace(id=uuid4(), transcript=[('customer', 'Call tomorrow', None)]))
    assert result['lead_status'] == 'CALLBACK' and result['preferred_callback_time'] == 'tomorrow'
    assert result['wants_course'] is None and result['ok_with_paid_course'] is None
    close.assert_awaited_once()

@pytest.mark.parametrize('label', ['CALLBACK', 'WARM', 'HOT', 'COLD', 'UNKNOWN'])
def test_explicit_rejection_overrides_interest_or_callback(label):
    result = ScreeningSummary(lead_status=label, explicitly_not_interested=True,
        preferred_callback_time='tomorrow', summary='Declined the course; agent offered a callback.')
    assert result.lead_status == 'NOT_INTERESTED'
    assert result.wants_course is False

@pytest.mark.parametrize('label', ['DO_NOT_CALL', 'WRONG_NUMBER', 'NOT_GENUINE'])
def test_rejection_preserves_higher_priority_outcomes(label):
    result = ScreeningSummary(lead_status=label, genuineness='DENIED',
        explicitly_not_interested=True, summary='Declined further contact.')
    assert result.lead_status == label


def test_callback_without_rejection_remains_callback():
    result = ScreeningSummary(lead_status='CALLBACK', preferred_callback_time='tomorrow',
        summary='Busy; requested tomorrow. Interest not assessed.')
    assert result.lead_status == 'CALLBACK'
    assert result.wants_course is None
