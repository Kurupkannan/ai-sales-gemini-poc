"""Private, session-bound prototype. No customer authentication or outbound messages."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, update, or_, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from app.api.leads import Session
from app.auth.dependencies import require_permission
from app.config import settings
from app.db.models.whatsapp import WhatsAppConnection as Connection, WhatsAppWebhookReceipt as Receipt
from app.whatsapp import security
from app.whatsapp.client import MetaClient, MetaError, asset_id

router = APIRouter(tags=["WhatsApp prototype"])
async def org_owner(request: Request, identity=Depends(require_permission('integrations.manage'))):
    return str(request.state.organization_id)
Owner = Annotated[str, Depends(org_owner)]
PREFIX = "/api/orgs/{org_id}/whatsapp"


def now(): return datetime.now(timezone.utc)

def integration_audit(session, action, ident):
    from app.db.models.identity import AuditEvent
    actor = session.info.get('identity')
    if actor:
        session.add(AuditEvent(actor_id=actor.id, organization_id=session.info['organization_id'],
            action=action, target=str(ident), details={}))


def public(row):
    return {k: getattr(row, k) for k in ("id", "state", "created_at", "expires_at", "token_expires_at",
            "candidate_wabas", "waba_id", "phone_number_id", "display_phone_number", "verified_name",
            "subscribed", "registered", "last_error", "last_webhook_at", "operation_until")}


async def owned(session, ident, owner):
    row = await session.scalar(select(Connection).where(Connection.id == ident, Connection.session_hash == owner))
    if not row: raise HTTPException(404, "Connection not found in this prototype session.")
    return row


async def claim(session, ident, owner):
    op = str(uuid4())
    row = (await session.execute(update(Connection).where(Connection.id == ident, Connection.session_hash == owner,
            Connection.token_ciphertext.is_not(None), Connection.state != "disconnected",
            or_(Connection.operation_until.is_(None), Connection.operation_until < now()))
            .values(operation_id=op, operation_until=now()+timedelta(minutes=10), last_error=None)
            .returning(Connection))).scalar_one_or_none()
    if not row:
        raise HTTPException(409, "Connection is unavailable or setup is already running. Refresh before retrying.")
    await session.commit()
    return row, op


async def finish(session, ident, op, **values):
    await session.execute(update(Connection).where(Connection.id == ident, Connection.operation_id == op,
                          Connection.state != "disconnected").values(**values, operation_id=None, operation_until=None))
    integration_audit(session, "whatsapp.updated", ident)
    await session.commit()


@router.get(PREFIX + "/status")
async def status(response: Response, owner: Owner):
    response.headers["Cache-Control"] = "no-store"
    missing = security.missing_settings()
    return {"enabled": settings.WHATSAPP_PROTOTYPE_ENABLED, "configured": not missing, "missing": missing,
            "app_id": settings.META_APP_ID, "config_id": settings.META_EMBEDDED_SIGNUP_CONFIG_ID,
            "api_version": settings.META_GRAPH_API_VERSION, "mode": "private-pilot"}


@router.post(PREFIX + "/session")
async def session_start(request: Request, response: Response, owner: Owner):
    return {"csrf": ""}


@router.post(PREFIX + "/connections/list")
async def connections(owner: Owner, session: Session, response: Response):
    response.headers["Cache-Control"] = "no-store"
    rows = (await session.scalars(select(Connection).where(Connection.session_hash == owner)
            .order_by(Connection.created_at.desc()).limit(20))).all()
    return [public(row) for row in rows]


@router.post(PREFIX + "/onboarding/start", status_code=201)
async def start(owner: Owner, session: Session, request: Request):
    # Supersede unused attempts for this session; only latest pending code is accepted.
    await session.execute(update(Connection).where(Connection.session_hash == owner, Connection.state == "pending")
                          .values(state="cancelled"))
    row = Connection(organization_id=UUID(owner), initiated_by_uid=request.state.identity.uid, session_hash=owner, expires_at=now()+timedelta(minutes=15))
    session.add(row)
    await session.flush()
    integration_audit(session, "whatsapp.onboarding_started", row.id)
    await session.commit()
    await session.refresh(row)
    return public(row)


class Code(BaseModel):
    code: SecretStr


@router.post(PREFIX + "/onboarding/{ident}/complete")
async def complete(ident: UUID, body: Code, owner: Owner, session: Session, request: Request):
    code = body.code.get_secret_value()
    if not code or len(code) > 8192: raise HTTPException(422, "Invalid authorization code.")
    row = (await session.execute(update(Connection).where(Connection.id == ident, Connection.session_hash == owner,
            Connection.state == "pending", Connection.initiated_by_uid == request.state.identity.uid, Connection.expires_at > now()).values(state="exchanging")
            .returning(Connection))).scalar_one_or_none()
    if not row: raise HTTPException(409, "Signup expired, was cancelled, or its code was already submitted. Start again.")
    await session.commit()  # Consume once BEFORE the provider call, including uncertain timeouts.
    try:
        token = await MetaClient().exchange(code)
        # Persist before introspection so transient verification errors can be retried.
        ciphertext = security.encrypt_token(token)
        await session.execute(update(Connection).where(Connection.id == ident, Connection.state == "exchanging")
                              .values(token_ciphertext=ciphertext, state="authorized"))
        await session.commit()
    except MetaError as exc:
        await session.execute(update(Connection).where(Connection.id == ident, Connection.state == "exchanging")
                              .values(state="failed", last_error=exc.message))
        await session.commit()
        raise HTTPException(502, exc.message) from None
    session.expire_all()
    return public(await owned(session, ident, owner))


@router.post(PREFIX + "/connections/{ident}/verify")
async def verify(ident: UUID, owner: Owner, session: Session):
    row, op = await claim(session, ident, owner)
    try:
        data = await MetaClient().inspect(security.decrypt_token(row.token_ciphertext))
        expiry = datetime.fromtimestamp(data["expires_at"], timezone.utc) if data["expires_at"] else None
        await finish(session, ident, op, scopes=data["scopes"], candidate_wabas=data["wabas"], token_expires_at=expiry,
                     state="connected" if row.subscribed else "choose_number", last_error=None)
    except (MetaError, HTTPException) as exc:
        msg = exc.message if isinstance(exc, MetaError) else exc.detail
        await finish(session, ident, op, state="action_required", last_error=msg)
        raise HTTPException(502, msg) from None
    session.expire_all()
    return public(await owned(session, ident, owner))


class Business(BaseModel):
    waba_id: str = Field(pattern=r"^[0-9]{1,40}$")


@router.post(PREFIX + "/connections/{ident}/numbers")
async def numbers(ident: UUID, body: Business, owner: Owner, session: Session):
    row = await owned(session, ident, owner)
    if row.state == "disconnected" or not row.token_ciphertext: raise HTTPException(409, "Reconnect first.")
    try:
        token = security.decrypt_token(row.token_ciphertext)
        await session.commit()  # Release the read transaction before provider I/O.
        await MetaClient().inspect(token)
        return await MetaClient().phones(token, body.waba_id)
    except MetaError as exc: raise HTTPException(502, exc.message) from None


class Selection(Business):
    phone_number_id: str = Field(pattern=r"^[0-9]{1,40}$")


@router.post(PREFIX + "/connections/{ident}/connect")
async def connect(ident: UUID, body: Selection, owner: Owner, session: Session):
    row, op = await claim(session, ident, owner)
    if row.phone_number_id and (row.phone_number_id != body.phone_number_id or row.waba_id != body.waba_id):
        await finish(session, ident, op)
        raise HTTPException(409, "Disconnect before selecting a different number.")
    try:
        token = security.decrypt_token(row.token_ciphertext)
        await MetaClient().inspect(token)
        phones = await MetaClient().phones(token, body.waba_id)
        phone = next((p for p in phones if p["id"] == body.phone_number_id), None)
        if not phone: raise MetaError("Selected phone number does not belong to the authorized WhatsApp account.")
        # Reserve verified ownership before subscribing. Unique phone ID prevents conflicting connections.
        await session.execute(update(Connection).where(Connection.id == ident, Connection.operation_id == op,
                              Connection.state != "disconnected").values(waba_id=body.waba_id, phone_number_id=phone["id"],
                              display_phone_number=phone["display_phone_number"], verified_name=phone["verified_name"]))
        await session.commit()
        await MetaClient().subscribe(token, body.waba_id)
        await finish(session, ident, op, state="connected", subscribed=True, last_error=None)
    except IntegrityError:
        await session.rollback()
        await finish(session, ident, op, state="action_required", last_error="This number is already connected. Disconnect its previous connection first.")
        raise HTTPException(409, "This number is already connected in the pilot. Use its existing session to disconnect first.") from None
    except (MetaError, HTTPException) as exc:
        msg = exc.message if isinstance(exc, MetaError) else exc.detail
        await finish(session, ident, op, state="action_required", last_error=msg)
        raise HTTPException(502, msg) from None
    session.expire_all()
    return public(await owned(session, ident, owner))


class Registration(BaseModel):
    pin: SecretStr
    confirm_new_cloud_number: bool = False


@router.post(PREFIX + "/connections/{ident}/register")
async def register(ident: UUID, body: Registration, owner: Owner, session: Session):
    import re
    pin = body.pin.get_secret_value()
    if not body.confirm_new_cloud_number or not re.fullmatch(r"\d{6}", pin):
        raise HTTPException(422, "Confirm this is a new Cloud API number and enter a six-digit registration PIN.")
    row, op = await claim(session, ident, owner)
    try:
        if not row.phone_number_id or not row.subscribed: raise MetaError("Connect a verified number before registration.")
        if row.registered: raise MetaError("This prototype already confirmed registration. No further registration is needed.")
        token = security.decrypt_token(row.token_ciphertext)
        await MetaClient().inspect(token)
        phones = await MetaClient().phones(token, row.waba_id)
        phone = next((p for p in phones if p["id"] == row.phone_number_id), None)
        if not phone or phone.get("code_verification_status") != "VERIFIED":
            raise MetaError("Complete phone ownership verification in Meta first.")
        await MetaClient().register(token, row.phone_number_id, pin)
        await finish(session, ident, op, registered=True, state="connected", last_error=None)
    except (MetaError, HTTPException) as exc:
        msg = exc.message if isinstance(exc, MetaError) else exc.detail
        await finish(session, ident, op, last_error=msg)
        raise HTTPException(502, msg) from None
    session.expire_all()
    return public(await owned(session, ident, owner))


@router.post(PREFIX + "/connections/{ident}/disconnect")
async def disconnect(ident: UUID, owner: Owner, session: Session):
    # Local disconnect only: shared WABA subscriptions must not be revoked indiscriminately.
    row = await owned(session, ident, owner)
    await session.execute(update(Connection).where(Connection.id == row.id).values(state="disconnected",
        token_ciphertext=None, phone_number_id=None, subscribed=False, registered=False, operation_id=None,
        operation_until=None, last_error=None))
    integration_audit(session, "whatsapp.disconnected", ident)
    await session.commit()
    return {"status": "disconnected", "note": "Local credential removed. Manage app access in Meta Business Integrations to revoke Meta's grant."}


@router.get("/webhooks/whatsapp")
async def challenge(request: Request):
    security.enabled()
    query = request.query_params
    if query.get("hub.mode") != "subscribe" or not hmac.compare_digest(query.get("hub.verify_token", ""), settings.META_WEBHOOK_VERIFY_TOKEN.get_secret_value()):
        raise HTTPException(403, "Webhook verification failed.")
    value = query.get("hub.challenge", "")
    if not value or len(value) > 1024: raise HTTPException(400, "Invalid challenge.")
    return PlainTextResponse(value)


@router.post("/webhooks/whatsapp")
async def webhook(request: Request, session: Session):
    security.enabled()
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 256 * 1024: raise HTTPException(413, "Webhook is too large.")
    expected = "sha256=" + hmac.new(settings.META_APP_SECRET.get_secret_value().encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("x-hub-signature-256", ""), expected):
        raise HTTPException(403, "Invalid webhook signature.")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("object") != "whatsapp_business_account" or not isinstance(body.get("entry"), list): raise ValueError()
        entries = body["entry"]
        wabas = [asset_id(entry["id"]) for entry in entries]
    except (ValueError, TypeError, KeyError, MetaError):
        raise HTTPException(400, "Invalid WhatsApp webhook.") from None
    digest = hashlib.sha256(raw).hexdigest()
    inserted = (await session.execute(insert(Receipt).values(digest=digest, entry_count=len(entries))
                    .on_conflict_do_nothing(index_elements=[Receipt.digest]).returning(Receipt.id))).scalar_one_or_none()
    if inserted:
        await session.execute(text("SELECT app.record_whatsapp_webhook(CAST(:assets AS text[]))"), {"assets": wabas})
    await session.commit()
    return {"received": True}
