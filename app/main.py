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
from fastapi import FastAPI, HTTPException, WebSocket, status, Depends, Request
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
    if settings.APP_ENV == "production" and not settings.AUTH_REQUIRE_ADMIN_MFA:
        raise RuntimeError("Production requires AUTH_REQUIRE_ADMIN_MFA=true")
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
    docs_url=None if settings.APP_ENV == "production" else "/docs",
    redoc_url=None if settings.APP_ENV == "production" else "/redoc",
    openapi_url=None if settings.APP_ENV == "production" else "/openapi.json",
    lifespan=lifespan,
)


from sqlalchemy.exc import IntegrityError, SQLAlchemyError
@app.exception_handler(IntegrityError)
async def conflict_error(request, exc):
    return JSONResponse({'detail': 'This change conflicts with an existing record. Refresh and retry.'}, status_code=409)

@app.exception_handler(SQLAlchemyError)
async def database_error(request, exc):
    return JSONResponse({'detail': 'Database unavailable. Check configuration and migrations.'}, status_code=503)

@app.middleware('http')
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control'] = 'no-store'
    return response

app.add_middleware(UploadSizeLimit)
app.include_router(leads_router)
from app.api.identity import router as identity_router
app.include_router(identity_router)
from app.api.whatsapp import router as whatsapp_router
app.include_router(whatsapp_router)
from app.whatsapp.middleware import WhatsAppLimits
app.add_middleware(WhatsAppLimits)

# Pydantic error responses must not echo authorization codes or registration PINs.
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if "/whatsapp" in request.url.path or "/invitations" in request.url.path:
        return JSONResponse({'detail': 'Invalid WhatsApp request. Check the required fields.'}, status_code=422)
    return await request_validation_exception_handler(request, exc)


class HealthResponse(BaseModel):
    status: str = "ok"


from app.auth.dependencies import require_permission
from app.auth.calls import authorize_call, claim_stream, monitor_stream, finish_call

class TestCallRequest(BaseModel):
    contact_id: str
    idempotency_key: str = Field(min_length=16, max_length=100)
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
    authorization = await claim_stream(websocket)
    if not authorization:
        await websocket.close(code=1008); return
    await monitor_stream(websocket, authorization, handle_exotel_websocket)


@app.post("/api/orgs/{org_id}/calls", response_model=TestCallResponse)
async def trigger_test_call(payload: TestCallRequest, request: Request, identity=Depends(require_permission("calls.create"))):
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

    stream_token = await authorize_call(request.state.organization_id, identity, payload)
    stream_url += "?token=" + stream_token
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

    logger.info("Initiating authorized outbound call for organization %s", request.state.organization_id)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                url,
                data=form_data,
                auth=auth,
                headers={"Accept": "application/json"},
            )

        if response.status_code >= 400:
            await finish_call(stream_token, "failed")
            logger.error("Exotel API error (status %d)", response.status_code)
            raise HTTPException(
                status_code=response.status_code,
                detail="Call provider rejected the request.",
            )

        # Parse Exotel response safely
        resp_json = response.json() if "application/json" in response.headers.get("content-type", "") else {}
        call_sid = None
        if isinstance(resp_json, dict):
            call_obj = resp_json.get("Call") or resp_json
            if isinstance(call_obj, dict):
                call_sid = call_obj.get("Sid")

        await finish_call(stream_token, "initiated", call_sid)
        logger.info("Call successfully queued with Exotel. CallSid: %s", call_sid)
        return TestCallResponse(
            status="initiated",
            message="Outbound call initiated successfully",
            stream_url=settings.websocket_stream_url,
            call_sid=call_sid,
            details=None,
        )

    except httpx.RequestError as exc:
        await finish_call(stream_token, "unknown")
        logger.error("Call provider request failed or timed out")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Call provider result is uncertain. Do not retry this request.",
        )


# A built frontend is served on the same origin; Vite proxies /api in development.
from pathlib import Path
from fastapi.staticfiles import StaticFiles

frontend_dist = Path(__file__).resolve().parents[2] / "fe" / "dist"
if frontend_dist.is_dir():
    app.mount("/ui", StaticFiles(directory=frontend_dist, html=True), name="lead-ui")
