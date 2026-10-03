from datetime import datetime
from uuid import UUID
from sqlalchemy import (
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
from app.db.base import Base, MutableRecord, Record


class CallSession(MutableRecord, Base):
    __tablename__ = "call_sessions"
    __table_args__ = (
        UniqueConstraint("id", "conversation_id"),
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
    provider_call_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    stream_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    end_reason: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TranscriptTurn(Record, Base):
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
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interrupted: Mapped[bool] = mapped_column(Boolean, server_default="false")
