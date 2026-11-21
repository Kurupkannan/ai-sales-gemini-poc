"""FastAPI application entry point.

Exposes:
- GET /health
- WebSocket /plivo/media
- POST /api/orgs/{org_id}/calls
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.leads import router as leads_router
from app.api.upload_limit import UploadSizeLimit
from app.config import settings
from app.db.session import database
from app.api.calls import router as calls_router
from app.plivo.webhooks import router as plivo_callbacks
from app.plivo.websocket import router as plivo_media
from app.telephony.worker import run as reconcile_calls
import asyncio
from contextlib import suppress

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
    if settings.OUTBOUND_CALLS_ENABLED:
        settings.validate_plivo()
    if settings.PUBLIC_BASE_URL:
        logger.info("Public backend origin configured")
    else:
        logger.warning("PUBLIC_BASE_URL is not set! Set it before triggering calls.")
    await database.prepare()
    if database.configured:
        from app.services.leads import release_unknown_consent
        if released := await release_unknown_consent():
            logger.info("Unblocked %s numbers that were only waiting for calling permission", released)
    worker = None
    if database.configured and settings.PLIVO_AUTH_ID and settings.PLIVO_AUTH_TOKEN.get_secret_value() and settings.PLIVO_ORGANIZATION_ID:
        worker = asyncio.create_task(reconcile_calls(), name="telephony_reconciler")
    from app.telephony.dialer import auto_dial_loop
    auto_dialer = asyncio.create_task(auto_dial_loop(), name="auto_dialer") if database.configured else None
    sheets_worker = None
    if settings.GOOGLE_SHEETS_ENABLED and database.configured:
        from app.services.google_sheets import configuration, run as sync_google_sheets
        configuration()
        sheets_worker = asyncio.create_task(sync_google_sheets(), name="google_sheets_export")
    try:
        yield
    finally:
        if sheets_worker:
            sheets_worker.cancel()
            with suppress(asyncio.CancelledError):
                await sheets_worker
        if auto_dialer:
            auto_dialer.cancel()
            with suppress(asyncio.CancelledError):
                await auto_dialer
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        from app.http_clients import close_clients
        await close_clients()
        await database.dispose()
        logger.info("Shutting down ai-sales-gemini-poc service")


app = FastAPI(
    title="AI Sales Gemini POC",
    description="Proof of concept: Plivo audio streaming <-> Gemini Live API",
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
    from app.db.session import log_database_error
    log_database_error(exc)
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
app.include_router(calls_router)
app.include_router(plivo_callbacks)
app.include_router(plivo_media)
from app.api.identity import router as identity_router
app.include_router(identity_router)
from app.api.whatsapp import router as whatsapp_router
app.include_router(whatsapp_router)
from app.whatsapp.middleware import WhatsAppLimits
app.add_middleware(WhatsAppLimits)
from app.timing import RequestTiming
app.add_middleware(RequestTiming)

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


@app.get("/")
async def root():
    """Root endpoint providing service information."""
    return {
        "service": "AI Sales Gemini POC",
        "status": "online",
        "health": "/health",
        "docs": "/docs",
        "websocket": "/plivo/media",
        "provider": "plivo",
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return {"status": "ok"}



# A built frontend is served on the same origin; Vite proxies /api in development.
from pathlib import Path
from fastapi.staticfiles import StaticFiles

frontend_dist = Path(__file__).resolve().parents[2] / "fe" / "dist"
if frontend_dist.is_dir():
    app.mount("/ui", StaticFiles(directory=frontend_dist, html=True), name="lead-ui")
