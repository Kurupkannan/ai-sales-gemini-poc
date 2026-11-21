"""Shared SQLAlchemy metadata and primary-key/timestamp columns."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC timestamps on PostgreSQL and SQLite alike.

    SQLite has no timezone type: values are stored as naive UTC and returned aware,
    so comparisons with datetime.now(timezone.utc) keep working.
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name != 'sqlite':
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(
        schema="app",
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        },
    )
    type_annotation_map = {datetime: UTCDateTime, dict: JSON, list: JSON}


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, server_default=func.now()
    )


class MutableRecord(Record):
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, server_default=func.now(), onupdate=utcnow
    )


class TenantRecord:
    """All business data requires an explicit organization."""
    # Organizations live in Firebase Realtime Database, so this is a plain indexed UUID.
    organization_id: Mapped[UUID] = mapped_column(index=True)
