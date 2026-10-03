from datetime import datetime
from uuid import UUID
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, MutableRecord, Record


class ConsentEvent(Record, Base):
    __tablename__ = "consent_events"
    __table_args__ = (
        CheckConstraint("action IN ('granted', 'revoked')", name="action"),
    )
    contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.lead_contacts.id"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(100))
    evidence: Mapped[dict] = mapped_column(JSONB, server_default="{}")


class ContactPreference(MutableRecord, Base):
    __tablename__ = "contact_preferences"
    __table_args__ = (UniqueConstraint("contact_id"),)
    contact_id: Mapped[UUID] = mapped_column(ForeignKey("app.lead_contacts.id"))
    suppressed: Mapped[bool] = mapped_column(Boolean, server_default="true")
    reason: Mapped[str] = mapped_column(String(200), server_default="consent_unknown")
