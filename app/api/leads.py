import json
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, func, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session, DatabaseConfigurationError
from app.db.models import Lead, LeadContact, ContactPreference, ImportBatch
from app.services.lead_sheets import read_sheet, suggested_mapping, missing_required, validate_mapping, SheetError, MAX_BYTES
from app.services.leads import prepare_import, commit_import, batch_dict

router = APIRouter(prefix="/api", tags=["Leads"])


async def session_dependency():
    try:
        async with asynccontextmanager(get_session)() as session:
            yield session
    except (SQLAlchemyError, DatabaseConfigurationError):
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
    version = await session.scalar(text("SELECT version_num FROM app.alembic_version"))
    if version != "0002":
        raise HTTPException(503, "Database needs migrations. Run alembic upgrade head.")
    await session.execute(select(Lead.id).limit(0))
    return {"status": "connected"}


@router.get("/leads/template")
async def template():
    return Response("name,phone,email,source,language,notes\r\n", media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="lead-template.csv"'})


@router.post("/imports/inspect")
async def inspect_file(file: UploadFile = File(...)):
    _, sheet = await uploaded(file)
    mapping = suggested_mapping(sheet.headers)
    # The client must ask the user to map any required column that was not detected.
    return {"headers": sheet.headers, "rows": len(sheet.rows), "worksheet": sheet.worksheet,
            "sample": [dict(zip(sheet.headers, cells)) for _, cells in sheet.rows[:3]], "mapping": mapping,
            "missing_required": missing_required(mapping)}


@router.post("/imports/preview")
async def preview(session: Session, file: UploadFile = File(...), mapping: str = Form(...), country: str = Form("IN")):
    _, sheet = await uploaded(file)
    parsed = mapping_value(mapping, sheet, country)
    _, result = await prepare_import(session, sheet, parsed, country)
    return result


@router.post("/imports", status_code=201)
async def import_file(session: Session, file: UploadFile = File(...), mapping: str = Form(...), country: str = Form("IN")):
    data, sheet = await uploaded(file)
    parsed = mapping_value(mapping, sheet, country)
    return await commit_import(session, sheet, data, file.filename or "leads", parsed, country)


@router.get("/imports")
async def imports(session: Session):
    items = (await session.scalars(select(ImportBatch).order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc()).limit(10))).all()
    return [batch_dict(batch) for batch in items]


@router.get("/leads")
async def leads(session: Session, q: str = Query("", max_length=200),
                status: Literal["new", "contacted", "qualified", "unqualified", "closed"] | None = None,
                page: int = Query(1, ge=1, le=100000), page_size: int = Query(20, ge=1, le=100)):
    phone = select(func.min(LeadContact.phone_number)).where(LeadContact.lead_id == Lead.id).scalar_subquery()
    query = select(Lead.id, Lead.name, Lead.email, Lead.source, Lead.status, Lead.preferred_language, Lead.created_at, phone.label("phone"))
    conditions = []
    if status:
        conditions.append(Lead.status == status)
    if q.strip():
        # Literal search rather than accepting wildcard patterns from the UI.
        pattern = '%' + q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        conditions.append(or_(Lead.name.ilike(pattern, escape='\\'), Lead.email.ilike(pattern, escape='\\'), Lead.source.ilike(pattern, escape='\\'),
                              Lead.id.in_(select(LeadContact.lead_id).where(LeadContact.phone_number.ilike(pattern, escape='\\')))))
    count = await session.scalar(select(func.count()).select_from(Lead).where(*conditions))
    items = (await session.execute(query.where(*conditions).order_by(Lead.created_at.desc(), Lead.id.desc()).offset((page - 1) * page_size).limit(page_size))).mappings().all()
    stats = dict((await session.execute(select(Lead.status, func.count()).group_by(Lead.status))).all())
    return {"items": [dict(row) for row in items], "total": count, "page": page, "page_size": page_size,
            "stats": {"total": sum(stats.values()), "new": stats.get("new", 0), "qualified": stats.get("qualified", 0)}}


@router.get("/leads/{lead_id}")
async def lead_detail(lead_id: UUID, session: Session):
    lead = await session.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found.")
    contacts = (await session.execute(select(LeadContact.phone_number, LeadContact.channel, LeadContact.verification_state,
                         ContactPreference.suppressed, ContactPreference.reason).outerjoin(ContactPreference, ContactPreference.contact_id == LeadContact.id)
                         .where(LeadContact.lead_id == lead_id).order_by(LeadContact.created_at))).mappings().all()
    batch = await session.get(ImportBatch, lead.import_batch_id) if lead.import_batch_id else None
    return {**{k: getattr(lead, k) for k in ("id", "name", "email", "source", "status", "preferred_language", "notes", "extra_fields", "created_at", "updated_at")},
            "contacts": [dict(row) for row in contacts], "import": batch_dict(batch) if batch else None}
