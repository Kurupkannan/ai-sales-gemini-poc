import hashlib
import hmac
import re
import secrets
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from app.config import settings

COOKIE = "wa_pilot_session"


def missing_settings():
    required = ("META_APP_ID", "META_APP_SECRET", "META_EMBEDDED_SIGNUP_CONFIG_ID",
                "META_GRAPH_API_VERSION", "META_WEBHOOK_VERIFY_TOKEN", "WHATSAPP_ENCRYPTION_KEY",
                "WHATSAPP_ALLOWED_ORIGINS", "DATABASE_URL")
    missing = []
    for name in required:
        value = getattr(settings, name)
        value = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        if not value.strip(): missing.append(name)
    if settings.META_GRAPH_API_VERSION and not re.fullmatch(r"v\d+\.0", settings.META_GRAPH_API_VERSION):
        missing.append("META_GRAPH_API_VERSION (expected vNN.0)")
    if settings.WHATSAPP_ENCRYPTION_KEY.get_secret_value():
        try: cipher()
        except HTTPException: missing.append("WHATSAPP_ENCRYPTION_KEY (invalid Fernet key)")
    for value in origins():
        parsed = urlsplit(value)
        if (parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username
                or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"))):
            missing.append("WHATSAPP_ALLOWED_ORIGINS (use exact HTTPS origins)")
        if not settings.WHATSAPP_COOKIE_SECURE and parsed.hostname not in ("localhost", "127.0.0.1"):
            missing.append("WHATSAPP_COOKIE_SECURE (required outside localhost)")
    for name in ("META_APP_ID", "META_EMBEDDED_SIGNUP_CONFIG_ID"):
        if getattr(settings, name) and not re.fullmatch(r"[0-9]{1,40}", getattr(settings, name)):
            missing.append(f"{name} (expected numeric ID)")
    return missing


def origins():
    return {part.strip() for part in settings.WHATSAPP_ALLOWED_ORIGINS.split(",") if part.strip()}


def enabled():
    if not settings.WHATSAPP_PROTOTYPE_ENABLED:
        raise HTTPException(503, "WhatsApp prototype is disabled. Complete backend setup first.")
    if missing_settings():
        raise HTTPException(503, "WhatsApp configuration is incomplete. See the setup checklist.")


def cipher():
    try: return Fernet(settings.WHATSAPP_ENCRYPTION_KEY.get_secret_value().encode())
    except (ValueError, TypeError):
        raise HTTPException(503, "Credential encryption is not configured correctly.") from None


def encrypt_token(value):
    return cipher().encrypt(("token:" + value).encode()).decode()


def decrypt_token(value):
    try:
        plain = cipher().decrypt(value.encode()).decode()
        if not plain.startswith("token:"): raise ValueError()
        return plain[6:]
    except (InvalidToken, ValueError, AttributeError):
        raise HTTPException(503, "Stored credential cannot be decrypted. Restore its encryption key or reconnect.") from None


def check_origin(request: Request):
    enabled()
    if request.headers.get("origin") not in origins():
        raise HTTPException(403, "Open the prototype from a configured frontend origin.")


def read_session(request):
    try:
        plain = cipher().decrypt(request.cookies.get(COOKIE, "").encode(), ttl=86400).decode()
        prefix, token = plain.split(":", 1)
        if prefix != "session" or len(token) != 64: raise ValueError()
        return token
    except (InvalidToken, ValueError):
        raise HTTPException(403, "Your prototype session expired. Reload this page.") from None


def new_session(request, response):
    check_origin(request)
    try: token = read_session(request)
    except HTTPException: token = secrets.token_hex(32)
    response.set_cookie(COOKIE, cipher().encrypt(("session:" + token).encode()).decode(),
                        max_age=86400, httponly=True, secure=settings.WHATSAPP_COOKIE_SECURE,
                        samesite="strict", path="/api/whatsapp")
    response.headers["Cache-Control"] = "no-store"
    return token


def authorized(request: Request):
    check_origin(request)
    token = read_session(request)
    if not hmac.compare_digest(token, request.headers.get("x-wa-csrf", "")):
        raise HTTPException(403, "Invalid prototype session. Reload this page.")
    return hashlib.sha256(token.encode()).hexdigest()
