"""Lead persistence and atomic spreadsheet import."""
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Lead, LeadContact, ContactPreference, ImportBatch
from app.services.lead_sheets import Sheet, normalize_rows, fingerprint


def batch_dict(batch):
    return {key: getattr(batch, key) for key in (
        "id", "filename", "created_at", "total_rows", "imported_rows",
        "duplicate_rows", "invalid_rows", "issues",
    )}


async def prepare_import(session: AsyncSession, sheet: Sheet, mapping: dict, country: str):
    valid, issues = normalize_rows(sheet, mapping, country)
    phones = [row["phone"] for row in valid]
    existing = set((await session.scalars(select(LeadContact.phone_number).where(LeadContact.phone_number.in_(phones)))).all()) if phones else set()
    accepted, duplicate_count = [], 0
    seen = set()
    for row in valid:
        phone = row["phone"]
        if phone in existing or phone in seen:
            duplicate_count += 1
            issues.append({"row": row["row"], "kind": "duplicate", "message": "Phone already exists in the lead list." if phone in existing else "Phone repeats an earlier valid row in this sheet."})
        else:
            accepted.append(row)
            seen.add(phone)
    invalid_count = len(sheet.rows) - len(valid)
    summary = {"total_rows": len(sheet.rows), "imported_rows": len(accepted), "duplicate_rows": duplicate_count,
               "invalid_rows": invalid_count, "issues": sorted(issues, key=lambda i: i["row"])[:100],
               "issues_truncated": len(issues) > 100, "preview": accepted[:10]}
    return accepted, summary


async def commit_import(session, sheet, data, filename, mapping, country):
    digest = fingerprint(data, mapping, country)
    async with session.begin():
        # Serializes imports across API processes; lock ends at transaction completion.
        await session.execute(text("SELECT pg_advisory_xact_lock(748392105)"))
        previous = await session.scalar(select(ImportBatch).where(ImportBatch.fingerprint == digest))
        if previous:
            return {**batch_dict(previous), "already_imported": True}
        accepted, summary = await prepare_import(session, sheet, mapping, country)
        batch = ImportBatch(id=uuid4(), filename=Path(filename.replace('\\', '/')).name[:255], fingerprint=digest,
                            mapping=mapping, country=country,
                            **{k: summary[k] for k in ("total_rows", "imported_rows", "duplicate_rows", "invalid_rows", "issues")})
        session.add(batch)
        await session.flush()
        leads, contacts, preferences = [], [], []
        for row in accepted:
            lead_id, contact_id = uuid4(), uuid4()
            leads.append({"id": lead_id, "name": row["name"] or None, "email": row["email"] or None,
                          "source": row["source"] or None, "preferred_language": row["preferred_language"] or None,
                          "notes": row["notes"] or None, "extra_fields": row["extra_fields"], "import_batch_id": batch.id})
            contacts.append({"id": contact_id, "lead_id": lead_id, "phone_number": row["phone"], "channel": "voice"})
            preferences.append({"id": uuid4(), "contact_id": contact_id, "suppressed": True, "reason": "consent_unknown"})
        if leads:
            await session.execute(Lead.__table__.insert(), leads)
            await session.execute(LeadContact.__table__.insert(), contacts)
            await session.execute(ContactPreference.__table__.insert(), preferences)
        return {**batch_dict(batch), "already_imported": False}
