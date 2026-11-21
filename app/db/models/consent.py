from app.db.base import TenantRecord
from datetime import datetime
from uuid import UUID
from sqlalchemy import (
    true, false,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy import JSON as JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, MutableRecord, Record, UTCDateTime


class ConsentEvent(TenantRecord, Record, Base):
    __tablename__ = "consent_events"
    __table_args__ = (
        CheckConstraint("action IN ('granted', 'revoked')", name="action"),
    )
    contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.lead_contacts.id"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(100))
    evidence: Mapped[dict] = mapped_column(JSONB, server_default="{}")


class ContactPreference(TenantRecord, MutableRecord, Base):
    __tablename__ = "contact_preferences"
    __table_args__ = (UniqueConstraint("contact_id"),)
    contact_id: Mapped[UUID] = mapped_column(ForeignKey("app.lead_contacts.id"))
    suppressed: Mapped[bool] = mapped_column(Boolean, server_default=true())
    reason: Mapped[str] = mapped_column(String(200), server_default="consent_unknown")
