from app.db.base import TenantRecord
from datetime import datetime
from uuid import UUID
from sqlalchemy import (
    true, false,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import JSON as JSONB
from app.db.base import Base, MutableRecord, Record, UTCDateTime


class CallSession(TenantRecord, MutableRecord, Base):
    __tablename__ = "call_sessions"
    __table_args__ = (
        UniqueConstraint("id", "conversation_id"),
        UniqueConstraint("organization_id", "idempotency_key"),
        UniqueConstraint("provider", "provider_account_id", "provider_call_id"),
        CheckConstraint(
            "status IN ('pending', 'initiated', 'active', 'completed', 'failed', 'unknown')",
            name="status",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at", name="time_order"
        ),
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.conversations.id"), index=True
    )
    provider_call_id: Mapped[str | None] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(20), default="plivo", server_default="plivo")
    provider_account_id: Mapped[str] = mapped_column(String(100), server_default="")
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    provider_status: Mapped[str | None] = mapped_column(String(100))
    bill_duration: Mapped[int | None] = mapped_column(Integer)
    stream_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    end_reason: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    result_summary: Mapped[dict | None] = mapped_column(JSONB)


class TranscriptTurn(TenantRecord, Record, Base):
    __tablename__ = "transcript_turns"
    __table_args__ = (
        ForeignKeyConstraint(
            ["call_session_id", "conversation_id"],
            ["app.call_sessions.id", "app.call_sessions.conversation_id"],
        ),
        UniqueConstraint("call_session_id", "sequence"),
        CheckConstraint("sequence >= 0", name="sequence"),
        CheckConstraint("speaker IN ('customer', 'agent', 'system')", name="speaker"),
    )
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("app.conversations.id"), index=True
    )
    call_session_id: Mapped[UUID] = mapped_column(index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    interrupted: Mapped[bool] = mapped_column(Boolean, server_default=false())
