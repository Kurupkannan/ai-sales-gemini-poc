from datetime import datetime
from uuid import UUID
from sqlalchemy import Index, String, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, Record

class User(Record, Base):
    __tablename__ = 'users'
    firebase_uid: Mapped[str] = mapped_column(String(128), unique=True)
    email: Mapped[str] = mapped_column(String(254))
    display_name: Mapped[str] = mapped_column(String(200), default='')
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true')
    platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    permissions_version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    claims_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claims_synced_version: Mapped[int] = mapped_column(Integer, default=0, server_default='0')

class Organization(Record, Base):
    __tablename__ = 'organizations'
    __table_args__ = (CheckConstraint("status IN ('pending','active','suspended','archived')", name='status'),)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    status: Mapped[str] = mapped_column(String(20), default='pending', server_default='pending')

class Membership(Record, Base):
    __tablename__ = 'organization_memberships'
    __table_args__ = (UniqueConstraint('organization_id', 'user_id'),
        CheckConstraint("role IN ('org_admin','member','viewer')", name='role'),
        CheckConstraint("status IN ('active','suspended','removed')", name='status'))
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('app.organizations.id'), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey('app.users.id'), index=True)
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default='active', server_default='active')

class Invitation(Record, Base):
    __tablename__ = 'organization_invitations'
    __table_args__ = (CheckConstraint("role IN ('org_admin','member','viewer')", name='role'),
        CheckConstraint("status IN ('pending','accepted','revoked')", name='status'),
        Index('uq_pending_org_email', 'organization_id', 'email', unique=True, postgresql_where=text("status = 'pending'")),)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('app.organizations.id'), index=True)
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[UUID] = mapped_column(ForeignKey('app.users.id'))
    status: Mapped[str] = mapped_column(String(20), default='pending')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[UUID | None] = mapped_column(ForeignKey('app.users.id'))

class AuditEvent(Record, Base):
    __tablename__ = 'audit_events'
    organization_id: Mapped[UUID | None] = mapped_column(ForeignKey('app.organizations.id'), index=True)
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey('app.users.id'))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(200))
    details: Mapped[dict] = mapped_column(JSONB, default=dict)

class EmailOutbox(Record, Base):
    __tablename__ = 'email_outbox'
    invitation_id: Mapped[UUID] = mapped_column(ForeignKey('app.organization_invitations.id'))
    payload: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class OutboundAuthorization(Record, Base):
    __tablename__ = 'outbound_authorizations'
    organization_id: Mapped[UUID] = mapped_column(ForeignKey('app.organizations.id'), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey('app.users.id'))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint('organization_id', 'idempotency_key'),)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default='pending')
    provider_call_id: Mapped[str | None] = mapped_column(String(200))
