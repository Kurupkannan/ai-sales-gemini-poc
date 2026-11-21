from app.db.base import TenantRecord
from uuid import UUID
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, MutableRecord


class LeadContact(TenantRecord, MutableRecord, Base):
    __tablename__ = "lead_contacts"
    __table_args__ = (
        # Portable shape check; normalize_number() enforces full E.164 before insert.
        CheckConstraint("substr(phone_number, 1, 1) = '+' AND length(phone_number) BETWEEN 3 AND 16", name="e164"),
        CheckConstraint("channel IN ('voice', 'whatsapp')", name="channel"),
        CheckConstraint(
            "verification_state IN ('unknown', 'verified', 'invalid')",
            name="verification",
        ),
        UniqueConstraint("lead_id", "phone_number", "channel"),
        UniqueConstraint("id", "lead_id"),
    )
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("app.leads.id"), index=True)
    phone_number: Mapped[str] = mapped_column(String(16), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    verification_state: Mapped[str] = mapped_column(
        String(20), server_default="unknown"
    )
