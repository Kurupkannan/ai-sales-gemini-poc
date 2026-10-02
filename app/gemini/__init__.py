"""Gemini Live API integration package."""

from app.gemini.agent_prompt import INITIAL_GREETING, SYSTEM_INSTRUCTION
from app.gemini.live_session import (
    GeminiLiveManager,
    build_live_connect_config,
)

__all__ = [
    "INITIAL_GREETING",
    "SYSTEM_INSTRUCTION",
    "GeminiLiveManager",
    "build_live_connect_config",
]

