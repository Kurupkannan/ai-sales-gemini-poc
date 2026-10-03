from datetime import datetime
from uuid import UUID
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, MutableRecord


class Conversation(MutableRecord, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["contact_id", "lead_id"],
            ["app.lead_contacts.id", "app.lead_contacts.lead_id"],
        ),
        CheckConstraint("channel IN ('voice', 'whatsapp')", name="channel"),
        CheckConstraint(
            "status IN ('pending', 'active', 'completed', 'failed')", name="status"
        ),
        CheckConstraint(
            "automation_mode IN ('automated', 'human', 'paused')",
            name="automation_mode",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name="time_order"
        ),
        Index("ix_conversations_lead_created", "lead_id", "created_at"),
    )
    lead_id: Mapped[UUID] = mapped_column(ForeignKey("app.leads.id"))
    contact_id: Mapped[UUID] = mapped_column(index=True)
    channel: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    automation_mode: Mapped[str] = mapped_column(String(20), server_default="automated")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
