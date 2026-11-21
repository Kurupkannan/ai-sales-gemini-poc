"""Tests for the webinar invitation script and persona configuration."""

from app.config import Settings
from datetime import datetime
from app.gemini.agent_prompt import IST, INITIAL_GREETING, NO_REPLY, SYSTEM_INSTRUCTION, YES_REPLY, greeting_for

AFTERNOON = datetime(2026, 10, 31, 15, 0, tzinfo=IST)


def test_opening_is_the_webinar_invitation():
    assert INITIAL_GREETING.startswith('ഹലോ നമസ്കാരം!')
    assert 'Trademax Academy' in INITIAL_GREETING
    assert 'Free Live Webinar' in INITIAL_GREETING
    assert 'കേരളത്തിലെ No.1 Trading Institute' in INITIAL_GREETING
    assert 'അരുൺ മുരളി' in INITIAL_GREETING
    assert len(INITIAL_GREETING) < 200  # About 8-10 seconds spoken; details come only when asked.
    assert INITIAL_GREETING.rstrip().endswith('ജോയിൻ ചെയ്യാമോ?')


def test_greeting_adds_the_lead_name_only_when_known():
    assert greeting_for('Asha Nair', INITIAL_GREETING, AFTERNOON).startswith('ഹലോ Asha, നമസ്കാരം!')
    assert greeting_for(None, INITIAL_GREETING, AFTERNOON) == INITIAL_GREETING


def test_script_replies_and_rules_are_in_the_instruction():
    assert YES_REPLY in SYSTEM_INSTRUCTION and NO_REPLY in SYSTEM_INSTRUCTION
    assert 'end_call' in SYSTEM_INSTRUCTION
    assert 'Completely free' in SYSTEM_INSTRUCTION
    assert 'Basics of Stock Market' in SYSTEM_INSTRUCTION
    assert 'SPOKEN KERALA MALAYALAM' in SYSTEM_INSTRUCTION


def test_config_defaults_use_agent_prompt():
    settings = Settings(_env_file=None)
    assert settings.INITIAL_GREETING == INITIAL_GREETING
    assert settings.SYSTEM_INSTRUCTION == SYSTEM_INSTRUCTION


def test_missing_or_placeholder_names_are_never_spoken():
    from app.gemini.agent_prompt import call_prompt, short_name
    for value in (None, '', 'NA', 'Unknown', '9876543210', 'Lead 12', 'test'):
        assert short_name(value) == ''
        assert greeting_for(value, INITIAL_GREETING, AFTERNOON) == INITIAL_GREETING
    assert short_name('K. R. Rahul') == 'Rahul'
    assert 'Do not ask for their name' in call_prompt('base', None, None, None)


def test_script_persuades_once_and_honestly():
    assert 'ONE short, warm' in SYSTEM_INSTRUCTION and 'Persuade only once' in SYSTEM_INSTRUCTION
    assert 'no fake urgency' in SYSTEM_INSTRUCTION
    assert 'NEVER PERSUADE when they ask not to be called' in SYSTEM_INSTRUCTION


def test_after_7pm_the_greeting_invites_them_to_join_now():
    evening = greeting_for(None, INITIAL_GREETING, datetime(2026, 10, 31, 19, 20, tzinfo=IST))
    assert 'ഇന്ന് രാത്രി 7 മണിക്ക്' not in evening and 'നടക്കുന്നുണ്ട്' in evening


def test_agent_knows_the_time_and_reads_intent():
    from app.gemini.agent_prompt import call_prompt
    assert 'CURRENT TIME: Saturday 31 October 2026, 03:00 PM IST' in call_prompt('b', None, None, None, AFTERNOON)
    assert '# BE SMART' in SYSTEM_INSTRUCTION and 'Never say the same sentence twice' in SYSTEM_INSTRUCTION
