from datetime import datetime
from uuid import UUID
from sqlalchemy import Index, String, Integer, Boolean, ForeignKey, UniqueConstraint, CheckConstraint, Text, text, true, false
from sqlalchemy import JSON as JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, Record, UTCDateTime

# Organizations, users, memberships and invitations live in Firebase Realtime Database
# (app.auth.store). Rows here keep their UUIDs as plain columns, without foreign keys.

class AuditEvent(Record, Base):
    __tablename__ = 'audit_events'
    organization_id: Mapped[UUID | None] = mapped_column(index=True)
    actor_id: Mapped[UUID | None] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)

class EmailOutbox(Record, Base):
    __tablename__ = 'email_outbox'
    invitation_id: Mapped[UUID] = mapped_column(index=True)
    payload: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime)

class OutboundAuthorization(Record, Base):
    __tablename__ = 'outbound_authorizations'
    organization_id: Mapped[UUID] = mapped_column(index=True)
    actor_id: Mapped[UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint('organization_id', 'idempotency_key'),)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    provider_call_id: Mapped[str | None] = mapped_column(String(200))

    # Call-attempt routing metadata; business data remains tenant scoped.
    provider: Mapped[str] = mapped_column(String(20), default='plivo', server_default='plivo')
    provider_account_id: Mapped[str | None] = mapped_column(String(100))
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    call_session_id: Mapped[UUID | None] = mapped_column(ForeignKey('app.call_sessions.id'), unique=True)
    from_number: Mapped[str | None] = mapped_column(String(16))
    to_number: Mapped[str | None] = mapped_column(String(16))
    token_ciphertext: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    stream_id: Mapped[str | None] = mapped_column(String(200))
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cleanup_needed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    reconcile_after: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    end_reason: Mapped[str | None] = mapped_column(String(200))
