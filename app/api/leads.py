import json
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, Request
from pydantic import BaseModel, Field
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, func, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import organization_access, require_permission
from app.db.session import get_session, DatabaseConfigurationError, database
from app.db.models import Lead, LeadContact, ContactPreference, ConsentEvent, ImportBatch, Conversation, CallSession
from app.services.lead_sheets import read_sheet, suggested_mapping, missing_required, validate_mapping, normalize_rows, Sheet, SheetError, MAX_BYTES
from app.services.leads import prepare_import, commit_import, batch_dict, default_preference

router = APIRouter(prefix="/api/orgs/{org_id}", tags=["Leads"], dependencies=[Depends(organization_access)])


class CallingPermission(BaseModel):
    allowed: bool
    evidence: str = Field(min_length=10, max_length=500)


class NewLead(BaseModel):
    phone: str = Field(min_length=7, max_length=30)
    country: str = "IN"
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=254)
    source: str = Field(default="", max_length=100)
    preferred_language: str = Field(default="", max_length=20)
    notes: str = Field(default="", max_length=2000)


async def session_dependency(request: Request):
    try:
        async with asynccontextmanager(get_session)() as session:
            session.info["organization_id"] = getattr(request.state, "organization_id", None)
            session.info["identity"] = getattr(request.state, "identity", None)
            yield session
    except (SQLAlchemyError, DatabaseConfigurationError) as exc:
        from app.db.session import log_database_error
        log_database_error(exc)
        raise HTTPException(503, "Database unavailable. Check the backend connection and run migrations, then retry.") from None


Session = Annotated[AsyncSession, Depends(session_dependency)]


async def uploaded(file):
    try:
        data = await file.read(MAX_BYTES + 1)
    finally:
        await file.close()
    try:
        sheet = await run_in_threadpool(read_sheet, data, file.filename or "")
    except SheetError as exc:
        raise HTTPException(422, str(exc)) from None
    return data, sheet


def mapping_value(raw, sheet, country):
    try:
        mapping = json.loads(raw)
        validate_mapping(sheet, mapping, country)
        return mapping
    except (ValueError, SheetError, TypeError) as exc:
        raise HTTPException(422, str(exc) if isinstance(exc, SheetError) else "Invalid column mapping.") from None


@router.get("/database/status")
async def database_status(session: Session):
    version = "0006" if database.is_sqlite else await session.scalar(text("SELECT version_num FROM app.alembic_version"))
    if version not in ("0006",):
        raise HTTPException(503, "Database needs migrations. Run alembic upgrade head.")
    await session.execute(select(Lead.id).limit(0))
    return {"status": "connected"}


@router.get("/leads/template")
async def template():
    return Response("name,phone,email,source,language,notes\r\n", media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="lead-template.csv"'})


@router.post("/imports/inspect", dependencies=[Depends(require_permission("leads.import"))])
async def inspect_file(file: UploadFile = File(...)):
    _, sheet = await uploaded(file)
    mapping = suggested_mapping(sheet.headers)
    # The client must ask the user to map any required column that was not detected.
    return {"headers": sheet.headers, "rows": len(sheet.rows), "worksheet": sheet.worksheet,
            "sample": [dict(zip(sheet.headers, cells)) for _, cells in sheet.rows[:3]], "mapping": mapping,
            "missing_required": missing_required(mapping)}


@router.post("/imports/preview", dependencies=[Depends(require_permission("leads.import"))])
async def preview(session: Session, file: UploadFile = File(...), mapping: str = Form(...), country: str = Form("IN")):
    _, sheet = await uploaded(file)
    parsed = mapping_value(mapping, sheet, country)
    _, result = await prepare_import(session, sheet, parsed, country)
    return result


@router.post("/imports", status_code=201, dependencies=[Depends(require_permission("leads.import"))])
async def import_file(session: Session, file: UploadFile = File(...), mapping: str = Form(...), country: str = Form("IN")):
    data, sheet = await uploaded(file)
    parsed = mapping_value(mapping, sheet, country)
    return await commit_import(session, sheet, data, file.filename or "leads", parsed, country)


@router.get("/imports")
async def imports(session: Session):
    items = (await session.scalars(select(ImportBatch).where(ImportBatch.organization_id == session.info["organization_id"]).order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc()).limit(10))).all()
    return [batch_dict(batch) for batch in items]


LeadType = Literal["HOT", "WARM", "COLD", "NOT_INTERESTED", "CALLBACK", "WRONG_NUMBER", "DO_NOT_CALL", "NOT_GENUINE", "NOT_ANSWERED", "NO_RESPONSE"]


def latest_lead_type():
    # Existing call results are authoritative; no duplicate status or backfill required.
    kind = CallSession.result_summary["lead_status"].as_string()
    return (select(kind).join(Conversation, Conversation.id == CallSession.conversation_id)
        .where(Conversation.lead_id == Lead.id,
               Conversation.organization_id == Lead.organization_id,
               CallSession.organization_id == Lead.organization_id,
               kind.in_(list(LeadType.__args__)))
        .order_by(CallSession.created_at.desc(), CallSession.id.desc()).limit(1)
        .correlate(Lead).scalar_subquery())


@router.get("/leads")
async def leads(session: Session, q: str = Query("", max_length=200),
                status: Literal["new", "contacted", "qualified", "unqualified", "closed"] | None = None,
                page: int = Query(1, ge=1, le=100000), page_size: int = Query(20, ge=1, le=100),
                lead_type: LeadType | None = None):
    phone = select(func.min(LeadContact.phone_number)).where(LeadContact.lead_id == Lead.id).scalar_subquery()
    eligible = select(LeadContact.id).join(ContactPreference, ContactPreference.contact_id == LeadContact.id).where(
        LeadContact.lead_id == Lead.id, LeadContact.channel == "voice", ContactPreference.suppressed.is_(False)).exists()
    query = select(Lead.id, Lead.name, Lead.email, Lead.source, Lead.status, Lead.preferred_language, Lead.created_at,
        phone.label("phone"), eligible.label("call_eligible"), latest_lead_type().label("lead_type"))
    conditions = [Lead.organization_id == session.info["organization_id"]]
    if status:
        conditions.append(Lead.status == status)
    if lead_type:
        conditions.append(latest_lead_type() == lead_type)
    if q.strip():
        # Literal search rather than accepting wildcard patterns from the UI.
        pattern = '%' + q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        conditions.append(or_(Lead.name.ilike(pattern, escape='\\'), Lead.email.ilike(pattern, escape='\\'), Lead.source.ilike(pattern, escape='\\'),
                              Lead.id.in_(select(LeadContact.lead_id).where(LeadContact.phone_number.ilike(pattern, escape='\\')))))
    stats = dict((await session.execute(select(Lead.status, func.count()).where(Lead.organization_id == session.info["organization_id"]).group_by(Lead.status))).all())
    # The existing status aggregate already supplies the exact count without search.
    count = (await session.scalar(select(func.count()).select_from(Lead).where(*conditions))
             if q.strip() or lead_type else stats.get(status, 0) if status else sum(stats.values()))
    items = (await session.execute(query.where(*conditions).order_by(Lead.created_at.desc(), Lead.id.desc()).offset((page - 1) * page_size).limit(page_size))).mappings().all()
    return {"items": [dict(row) for row in items], "total": count, "page": page, "page_size": page_size,
            "stats": {"total": sum(stats.values()), "new": stats.get("new", 0), "qualified": stats.get("qualified", 0)}}


@router.post("/leads", status_code=201, dependencies=[Depends(require_permission("leads.import"))])
async def create_lead(payload: NewLead, request: Request):
    headers = ["name", "phone", "email", "source", "preferred_language", "notes"]
    sheet = Sheet(headers=headers, rows=[(1, [str(getattr(payload, field)).strip() for field in headers])])
    try:
        valid, issues = normalize_rows(sheet, {field: field for field in headers}, payload.country)
    except SheetError as exc:
        raise HTTPException(422, str(exc)) from None
    if issues:
        raise HTTPException(422, issues[0]["message"])
    values = valid[0]
    org_id = request.state.organization_id
    async with database.transaction() as session:
        session.info["organization_id"] = org_id
        if not database.is_sqlite:  # SQLite transactions already hold the write lock.
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:org, 0))"), {"org": str(org_id)})
        existing = await session.scalar(select(LeadContact.id).where(
            LeadContact.organization_id == org_id, LeadContact.phone_number == values["phone"]))
        if existing:
            raise HTTPException(409, "This phone number already exists in the lead list.")
        lead = Lead(organization_id=org_id, name=values["name"] or None, email=values["email"] or None,
            source=values["source"] or None, preferred_language=values["preferred_language"] or None,
            notes=values["notes"] or None, extra_fields={})
        session.add(lead)
        await session.flush()
        contact = LeadContact(organization_id=org_id, lead_id=lead.id,
            phone_number=values["phone"], channel="voice")
        session.add(contact)
        await session.flush()
        session.add(ContactPreference(organization_id=org_id, contact_id=contact.id, **default_preference()))
        result = {"id": str(lead.id), "contact_id": str(contact.id), "phone": contact.phone_number}
    return result


@router.get("/leads/{lead_id}")
async def lead_detail(lead_id: UUID, session: Session):
    row = (await session.execute(select(Lead, latest_lead_type()).where(Lead.id == lead_id, Lead.organization_id == session.info["organization_id"]))).first()
    if not row:
        raise HTTPException(404, "Lead not found.")
    lead, lead_type = row
    contacts = (await session.execute(select(LeadContact.id, LeadContact.phone_number, LeadContact.channel, LeadContact.verification_state,
                         ContactPreference.suppressed, ContactPreference.reason).outerjoin(ContactPreference, ContactPreference.contact_id == LeadContact.id)
                         .where(LeadContact.lead_id == lead_id).order_by(LeadContact.created_at))).mappings().all()
    batch = await session.get(ImportBatch, lead.import_batch_id) if lead.import_batch_id else None
    return {**{k: getattr(lead, k) for k in ("id", "name", "email", "source", "status", "preferred_language", "notes", "extra_fields", "created_at", "updated_at")},
            "lead_type": lead_type,
            "call_eligible": any(row["channel"] == "voice" and row["suppressed"] is False for row in contacts),
            "contacts": [dict(row) for row in contacts], "import": batch_dict(batch) if batch else None}


@router.post("/leads/{lead_id}/contacts/{contact_id}/calling-permission", dependencies=[Depends(require_permission("calls.create"))])
async def set_calling_permission(lead_id: UUID, contact_id: UUID, payload: CallingPermission, request: Request):
    if len(payload.evidence.strip()) < 10:
        raise HTTPException(422, "Describe the permission or revocation in at least 10 characters.")
    org_id = request.state.organization_id
    async with database.transaction() as session:
        session.info["organization_id"] = org_id
        contact = await session.scalar(select(LeadContact).where(
            LeadContact.id == contact_id, LeadContact.lead_id == lead_id,
            LeadContact.organization_id == org_id).with_for_update())
        if not contact:
            raise HTTPException(404, "Contact not found.")
        if contact.channel != "voice":
            raise HTTPException(422, "Calling permission applies to voice contacts only.")
        preference = await session.scalar(select(ContactPreference).where(ContactPreference.contact_id == contact_id))
        if not preference:
            preference = ContactPreference(organization_id=org_id, contact_id=contact_id)
            session.add(preference)
        preference.suppressed = not payload.allowed
        preference.reason = "permission_recorded" if payload.allowed else "permission_revoked"
        session.add(ConsentEvent(organization_id=org_id, contact_id=contact_id, purpose="voice_outreach",
            action="granted" if payload.allowed else "revoked", occurred_at=datetime.now(timezone.utc),
            source="operator_recorded", evidence={"note": payload.evidence.strip(), "actor_id": str(request.state.identity.id)}))
    return {"contact_id": str(contact_id), "suppressed": not payload.allowed}
