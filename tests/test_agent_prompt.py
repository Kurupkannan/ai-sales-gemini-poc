"""Tests for agent prompt module and persona configuration."""

from app.config import settings
from app.gemini.agent_prompt import INITIAL_GREETING, SYSTEM_INSTRUCTION


def test_agent_prompt_greeting_content():
    assert "ട്രേഡ് മാക്സ് അക്കാദമിയിൽ നിന്നാണ് വിളിക്കുന്നത്" in INITIAL_GREETING
    assert "സംസാരിക്കാൻ പറ്റുമോ" in INITIAL_GREETING


def test_agent_prompt_system_instruction_key_requirements():
    # Persona & Company
    assert "Trade Max Academy" in SYSTEM_INSTRUCTION
    assert "Kerala" in SYSTEM_INSTRUCTION

    # Core Job & Tone Rules
    assert "YOUR ONLY JOB" in SYSTEM_INSTRUCTION
    assert "SPOKEN KERALA MALAYALAM" in SYSTEM_INSTRUCTION


def test_config_defaults_use_agent_prompt():
    assert settings.INITIAL_GREETING == INITIAL_GREETING
    assert settings.SYSTEM_INSTRUCTION == SYSTEM_INSTRUCTION
