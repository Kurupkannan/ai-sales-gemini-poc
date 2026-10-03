from uuid import UUID
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, MutableRecord


class LeadContact(MutableRecord, Base):
    __tablename__ = "lead_contacts"
    __table_args__ = (
        CheckConstraint("phone_number ~ '^\\+[1-9][0-9]{1,14}$'", name="e164"),
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
