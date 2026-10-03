"""FastAPI application entry point.

Exposes:
- GET /health
- WebSocket /exotel/media
- POST /test-call
"""

import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, status
from pydantic import BaseModel, Field

from app.api.leads import router as leads_router
from app.api.upload_limit import UploadSizeLimit
from app.config import settings
from app.db.session import database
from app.exotel.websocket import handle_exotel_websocket

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ai-sales-gemini-poc service")
    logger.info("Gemini Model configured: %s", settings.GEMINI_LIVE_MODEL)
    logger.info("Exotel Region: %s", settings.EXOTEL_REGION)
    if settings.PUBLIC_BASE_URL:
        logger.info("Configured StreamUrl: %s", settings.websocket_stream_url)
    else:
        logger.warning("PUBLIC_BASE_URL is not set! Set it before triggering calls.")
    try:
        yield
    finally:
        await database.dispose()
        logger.info("Shutting down ai-sales-gemini-poc service")


app = FastAPI(
    title="AI Sales Gemini POC",
    description="Proof of concept: Exotel AgentStream <-> Gemini Live API",
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(UploadSizeLimit)
app.include_router(leads_router)

class HealthResponse(BaseModel):
    status: str = "ok"


class TestCallRequest(BaseModel):
    to_number: str = Field(
        ...,
        description="Customer phone number with country code, e.g. +91XXXXXXXXXX or 0XXXXXXXXXX",
        examples=["+919876543210"],
    )


class TestCallResponse(BaseModel):
    status: str
    message: str
    stream_url: str
    call_sid: str | None = None
    details: dict[str, Any] | None = None


@app.get("/")
async def root():
    """Root endpoint providing service information."""
    return {
        "service": "AI Sales Gemini POC",
        "status": "online",
        "health": "/health",
        "docs": "/docs",
        "websocket": "/exotel/media",
        "stream_url": settings.websocket_stream_url,
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}



@app.websocket("/exotel/media")
async def websocket_exotel_media(websocket: WebSocket):
    """Bidirectional WebSocket endpoint for Exotel AgentStream."""
    await handle_exotel_websocket(websocket)


@app.post("/test-call", response_model=TestCallResponse)
async def trigger_test_call(payload: TestCallRequest):
    """Triggers an outbound phone call via Exotel's Connect Voice AI API."""
    stream_url = settings.websocket_stream_url
    if not stream_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "PUBLIC_BASE_URL is not configured in .env. Exotel requires a public "
                "wss:// URL (e.g. from ngrok) to establish the WebSocket."
            ),
        )

    if not settings.EXOTEL_API_KEY or not settings.EXOTEL_API_TOKEN or not settings.EXOTEL_ACCOUNT_SID:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Exotel credentials (EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_ACCOUNT_SID) are not configured.",
        )

    if not settings.EXOTEL_EXOPHONE:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="EXOTEL_EXOPHONE is not configured.",
        )

    url = settings.exotel_connect_url
    auth = (settings.EXOTEL_API_KEY, settings.EXOTEL_API_TOKEN)

    # Sanitize and format phone numbers for Exotel
    clean_to = payload.to_number.strip().replace(" ", "").replace("-", "")
    if len(clean_to) == 10 and not clean_to.startswith("+") and not clean_to.startswith("0"):
        clean_to = "0" + clean_to

    clean_caller_id = settings.EXOTEL_EXOPHONE.replace("-", "").replace(" ", "").strip()

    # Exotel Connect Voice AI API form parameters
    form_data = {
        "From": clean_to,
        "CallerId": clean_caller_id,
        "StreamUrl": stream_url,
        "StreamType": "bidirectional",
    }

    logger.info(
        "Initiating outbound call to %s via Exotel ExoPhone %s with StreamUrl %s",
        payload.to_number,
        settings.EXOTEL_EXOPHONE,
        stream_url,
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                url,
                data=form_data,
                auth=auth,
                headers={"Accept": "application/json"},
            )

        if response.status_code >= 400:
            logger.error("Exotel API error (status %d): %s", response.status_code, response.text)
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Exotel API returned error {response.status_code}: {response.text}",
            )

        # Parse Exotel response safely
        resp_json = response.json() if "application/json" in response.headers.get("content-type", "") else {}
        call_sid = None
        if isinstance(resp_json, dict):
            call_obj = resp_json.get("Call") or resp_json
            if isinstance(call_obj, dict):
                call_sid = call_obj.get("Sid")

        logger.info("Call successfully queued with Exotel. CallSid: %s", call_sid)
        return TestCallResponse(
            status="initiated",
            message="Outbound call initiated successfully",
            stream_url=stream_url,
            call_sid=call_sid,
            details=resp_json if resp_json else {"raw_response": response.text[:200]},
        )

    except httpx.RequestError as exc:
        logger.error("Failed to connect to Exotel API: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to communicate with Exotel: {exc}",
        )


# A built frontend is served on the same origin; Vite proxies /api in development.
from pathlib import Path
from fastapi.staticfiles import StaticFiles

frontend_dist = Path(__file__).resolve().parents[2] / "fe" / "dist"
if frontend_dist.is_dir():
    app.mount("/ui", StaticFiles(directory=frontend_dist, html=True), name="lead-ui")
