"""Application configuration loaded from environment variables."""

from pathlib import Path
from urllib.parse import urlparse
from typing import Literal
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


from app.gemini.agent_prompt import (
    INITIAL_GREETING as DEFAULT_INITIAL_GREETING,
    SYSTEM_INSTRUCTION as DEFAULT_SYSTEM_INSTRUCTION,
)


LOCAL_DATABASE_URL = "sqlite+aiosqlite:///" + str(Path(__file__).resolve().parents[1] / "data" / "ai_sales.db")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_ENV: Literal["development", "staging", "production"] = "development"
    FIREBASE_PROJECT_ID: str = ""
    GOOGLE_APPLICATION_CREDENTIALS: str = ""
    AUTH_REQUIRE_ADMIN_MFA: bool = False
    # Organizations, users, memberships and invitations live in Firebase Realtime Database.
    FIREBASE_DATABASE_URL: str = ""
    IDENTITY_CACHE_SECONDS: float = Field(default=30, ge=0, le=600)
    # Some networks advertise IPv6 but cannot reach Google over it; Python then stalls ~60s per request.
    NETWORK_IPV4_ONLY: bool = False
    APP_FRONTEND_URL: str = "http://localhost:5173/ui/"
    AUTH_OUTBOX_KEY: SecretStr = SecretStr("")
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: SecretStr = SecretStr("")
    SMTP_FROM: str = ""
    SMTP_STARTTLS: bool = True

    # Backend-only Google Sheets export. One configured organization per sheet.
    GOOGLE_SHEETS_ENABLED: bool = False
    GOOGLE_SHEETS_WEB_APP_URL: SecretStr = SecretStr("")
    GOOGLE_SHEETS_ACCESS_CODE: SecretStr = SecretStr("")
    GOOGLE_SHEETS_ORGANIZATION_ID: str = ""
    GOOGLE_SHEETS_SYNC_INTERVAL_SECONDS: int = Field(default=15, ge=5, le=3600)

    # Local SQLite file by default; a postgresql:// URL still works (credentials never appear in repr).
    DATABASE_URL: SecretStr = SecretStr(LOCAL_DATABASE_URL)
    DATABASE_PASSWORD: SecretStr = SecretStr("")
    MIGRATION_DATABASE_URL: SecretStr = SecretStr("")
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=50)
    DB_MAX_OVERFLOW: int = Field(default=0, ge=0, le=50)
    DB_POOL_TIMEOUT_SECONDS: int = Field(default=10, ge=1)
    DB_CONNECT_TIMEOUT_SECONDS: int = Field(default=10, ge=1)
    DB_IO_TIMEOUT_SECONDS: int = Field(default=20, ge=1)
    DB_POOL_RECYCLE_SECONDS: int = Field(default=300, ge=1)
    AUTH_HTTP_TIMEOUT_SECONDS: int = Field(default=10, ge=1)
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=15000, ge=1)
    DB_SSLMODE: Literal["verify-full", "disable"] = "verify-full"
    DB_SSLROOTCERT: str = ""

    # Private Embedded Signup prototype; never enable on a public operator API.
    WHATSAPP_PROTOTYPE_ENABLED: bool = False
    META_APP_ID: str = ""
    META_APP_SECRET: SecretStr = SecretStr("")
    META_EMBEDDED_SIGNUP_CONFIG_ID: str = ""
    META_GRAPH_API_VERSION: str = ""
    META_WEBHOOK_VERIFY_TOKEN: SecretStr = SecretStr("")
    WHATSAPP_ENCRYPTION_KEY: SecretStr = SecretStr("")
    WHATSAPP_ALLOWED_ORIGINS: str = ""
    WHATSAPP_COOKIE_SECURE: bool = True

    # Gemini settings
    GEMINI_API_KEY: str = Field(default="", description="Google Gemini API key")
    GEMINI_LIVE_MODEL: str = Field(default="gemini-3.8-live", description="Gemini Live model name")
    GEMINI_SUMMARY_MODEL: str = Field(default="gemini-2.5-flash", description="Post-call summary model")
    # Languages callers speak; hints for speech recognition (otherwise Malayalam is misheard as Spanish etc.).
    TRANSCRIPTION_LANGUAGES: str = Field(default="ml-IN,en-IN,hi-IN")
    VOICE_NAME: str = Field(default="Aoede", description="Voice name for Gemini prebuilt speech response")

    # TTS settings for natural Malayalam Neural voice
    TTS_MODE: bool = Field(default=True, description="Enable ultra-realistic Malayalam Neural TTS mode")
    TTS_VOICE: str = Field(default="ml-IN-SobhanaNeural", description="Malayalam Neural TTS voice (ml-IN-SobhanaNeural or ml-IN-MidhunNeural)")

    # Plivo outbound calling. Disabled until credentials and public ingress are ready.
    OUTBOUND_CALLS_ENABLED: bool = False
    # False: imported/added numbers can be called immediately. True: record permission per contact first.
    CALLS_REQUIRE_PERMISSION: bool = False
    PLIVO_AUTH_ID: str = ""
    PLIVO_AUTH_TOKEN: SecretStr = SecretStr("")
    PLIVO_FROM_NUMBER: str = ""
    # Caller-ID pool for concurrent calls, comma separated (+countrycode). Falls back to PLIVO_FROM_NUMBER.
    PLIVO_FROM_NUMBERS: str = ""
    PLIVO_ORGANIZATION_ID: str = ""
    PLIVO_ALLOWED_COUNTRIES: str = "IN"
    MEDIA_GRANT_ENCRYPTION_KEY: SecretStr = SecretStr("")
    CALL_MAX_DURATION_SECONDS: int = Field(default=600, ge=30, le=3600)
    CALL_ANSWER_TIMEOUT_SECONDS: int = Field(default=45, ge=10, le=120)
    CALL_GREETING_PREPARE_TIMEOUT_SECONDS: float = Field(default=10, gt=0, le=30)
    MEDIA_START_TIMEOUT_SECONDS: int = Field(default=10, ge=2, le=60)
    MEDIA_IDLE_TIMEOUT_SECONDS: int = Field(default=30, ge=5, le=120)
    # Silence on an answered call: check in after this long, then close, then hang up.
    CALL_SILENCE_SECONDS: float = Field(default=7, ge=3, le=60)
    # Caller audio louder than this (RMS of 16-bit samples) counts as the caller talking.
    # If the caller spoke and the agent has not started replying after this long, prompt it to answer.
    CALL_REPLY_NUDGE_SECONDS: float = Field(default=3, ge=1, le=30)
    # Caller talking over the agent for this long stops the agent's audio (barge-in).
    CALL_BARGE_IN_SECONDS: float = Field(default=0.4, ge=0.1, le=3)
    CALLER_VOICE_RMS: float = Field(default=600, ge=50, le=10000)
    # Answered calls are told to close at this point, and hung up CALL_WRAP_UP_GRACE_SECONDS later.
    CALL_WRAP_UP_SECONDS: float = Field(default=75, ge=20, le=600)
    CALL_WRAP_UP_GRACE_SECONDS: float = Field(default=20, ge=5, le=120)
    CALL_HANGUP_GRACE_SECONDS: float = Field(default=12, gt=0, le=60)
    MEDIA_GRANT_TTL_SECONDS: int = Field(default=300, ge=30, le=600)
    # Effective concurrency is min(this cap, numbers x TELEPHONY_CALLS_PER_NUMBER).
    TELEPHONY_MAX_CONCURRENT_CALLS: int = Field(default=30, ge=1, le=100)
    TELEPHONY_CALLS_PER_NUMBER: int = Field(default=1, ge=1, le=10)
    TELEPHONY_DIALS_PER_SECOND: float = Field(default=1, gt=0, le=10)
    # Auto-dial: how often to look for new leads, and how many to queue per round.
    AUTO_DIAL_INTERVAL_SECONDS: float = Field(default=15, ge=5, le=600)
    AUTO_DIAL_BATCH_SIZE: int = Field(default=25, ge=1, le=500)
    # Unanswered / silent calls are redialled automatically: at most this many retries, this far apart.
    CALL_RETRY_MAX: int = Field(default=2, ge=0, le=10)
    CALL_RETRY_AFTER_MINUTES: float = Field(default=30, ge=1, le=1440)
    TELEPHONY_HOURLY_CALL_LIMIT: int = Field(default=200, ge=1, le=10000)

    # Server settings
    PUBLIC_BASE_URL: str = Field(
        default="",
        description="Publicly accessible HTTPS origin of the server (e.g., from ngrok)",
    )
    PORT: int = Field(default=8000, description="Local server port")
    HOST: str = Field(default="0.0.0.0", description="Local server host")
    LOG_LEVEL: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")

    # Greeting & Persona instructions
    INITIAL_GREETING: str = Field(
        default=DEFAULT_INITIAL_GREETING,
        description="Initial greeting spoken to the customer upon call connect",
    )
    SYSTEM_INSTRUCTION: str = Field(
        default=DEFAULT_SYSTEM_INSTRUCTION,
        description="System instruction for Gemini Live persona",
    )

    @property
    def public_origin(self) -> str:
        value = self.PUBLIC_BASE_URL.strip().rstrip("/")
        parsed = urlparse(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin with no path, query or credentials.")
        return value

    @property
    def from_numbers(self) -> list[str]:
        numbers = [n.strip() for n in (self.PLIVO_FROM_NUMBERS or self.PLIVO_FROM_NUMBER).split(",")]
        return list(dict.fromkeys(n for n in numbers if n))

    @property
    def call_capacity(self) -> int:
        return min(self.TELEPHONY_MAX_CONCURRENT_CALLS, len(self.from_numbers) * self.TELEPHONY_CALLS_PER_NUMBER)

    @property
    def websocket_stream_url(self) -> str:
        return self.public_origin.replace("https://", "wss://", 1) + "/plivo/media"

    def validate_plivo(self) -> None:
        from cryptography.fernet import Fernet
        from uuid import UUID
        import re
        self.public_origin
        if not re.fullmatch(r"[A-Za-z0-9]+", self.PLIVO_AUTH_ID) or not self.PLIVO_AUTH_TOKEN.get_secret_value():
            raise ValueError("Set PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN.")
        if not self.from_numbers or not all(re.fullmatch(r"\+[1-9][0-9]{7,14}", n) for n in self.from_numbers):
            raise ValueError("Set PLIVO_FROM_NUMBERS to your purchased numbers in +countrycode format, comma separated.")
        try:
            UUID(self.PLIVO_ORGANIZATION_ID)
            Fernet(self.MEDIA_GRANT_ENCRYPTION_KEY.get_secret_value().encode())
        except (ValueError, TypeError):
            raise ValueError("Set PLIVO_ORGANIZATION_ID and a valid MEDIA_GRANT_ENCRYPTION_KEY.") from None
        if not self.DATABASE_URL.get_secret_value() or not self.GEMINI_API_KEY:
            raise ValueError("Calling requires DATABASE_URL and GEMINI_API_KEY.")


settings = Settings()
