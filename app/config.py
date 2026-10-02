"""Application configuration loaded from environment variables."""

from urllib.parse import urlparse
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


from app.gemini.agent_prompt import (
    INITIAL_GREETING as DEFAULT_INITIAL_GREETING,
    SYSTEM_INSTRUCTION as DEFAULT_SYSTEM_INSTRUCTION,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gemini settings
    GEMINI_API_KEY: str = Field(default="", description="Google Gemini API key")
    GEMINI_LIVE_MODEL: str = Field(default="gemini-3.8-live", description="Gemini Live model name")
    VOICE_NAME: str = Field(default="Aoede", description="Voice name for Gemini prebuilt speech response")

    # TTS settings for natural Malayalam Neural voice
    TTS_MODE: bool = Field(default=True, description="Enable ultra-realistic Malayalam Neural TTS mode")
    TTS_VOICE: str = Field(default="ml-IN-SobhanaNeural", description="Malayalam Neural TTS voice (ml-IN-SobhanaNeural or ml-IN-MidhunNeural)")

    # Exotel settings
    EXOTEL_API_KEY: str = Field(default="", description="Exotel API Key")
    EXOTEL_API_TOKEN: str = Field(default="", description="Exotel API Token")
    EXOTEL_ACCOUNT_SID: str = Field(default="", description="Exotel Account SID")
    EXOTEL_EXOPHONE: str = Field(default="", description="Exotel ExoPhone / Virtual caller ID")
    EXOTEL_REGION: str = Field(default="india", description="Exotel region: india or singapore")

    # Server settings
    PUBLIC_BASE_URL: str = Field(
        default="",
        description="Publicly accessible HTTPS/WSS URL of the server (e.g., from ngrok)",
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
    def websocket_stream_url(self) -> str:
        """Derives the public WebSocket URL for Exotel AgentStream.

        Converts http(s):// to ws(s):// and appends /exotel/media.
        """
        raw_url = self.PUBLIC_BASE_URL.strip().rstrip("/")
        if not raw_url:
            return ""

        parsed = urlparse(raw_url)
        scheme = parsed.scheme.lower()
        if scheme == "http":
            ws_scheme = "ws"
        elif scheme == "https":
            ws_scheme = "wss"
        elif scheme in ("ws", "wss"):
            ws_scheme = scheme
        else:
            ws_scheme = "wss"

        netloc = parsed.netloc or parsed.path.split("/")[0]
        return f"{ws_scheme}://{netloc}/exotel/media"

    @property
    def exotel_connect_url(self) -> str:
        """Returns the Exotel Connect Call API URL based on configured region."""
        region = self.EXOTEL_REGION.lower().strip()
        account_sid = self.EXOTEL_ACCOUNT_SID.strip()

        if "singapore" in region or region in ("sg", "global"):
            base = "https://api.exotel.com"
        else:
            # Default to India cluster
            base = "https://api.in.exotel.com"

        return f"{base}/v1/Accounts/{account_sid}/Calls/connect.json"


settings = Settings()
