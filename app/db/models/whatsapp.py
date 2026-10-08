from app.db.base import TenantRecord
"""Private pilot connection and durable webhook receipt metadata (no message bodies)."""
from datetime import datetime
from sqlalchemy import DateTime, String, Text, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, Record


class WhatsAppConnection(TenantRecord, Record, Base):
    __tablename__ = "whatsapp_connections"
    initiated_by_uid: Mapped[str | None] = mapped_column(String(128))
    workspace: Mapped[str] = mapped_column(String(40), server_default="private-pilot")
    session_hash: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(32), server_default="pending")
    token_ciphertext: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list] = mapped_column(JSONB, server_default="[]")
    candidate_wabas: Mapped[list] = mapped_column(JSONB, server_default="[]")
    waba_id: Mapped[str | None] = mapped_column(String(40))
    phone_number_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    display_phone_number: Mapped[str | None] = mapped_column(String(80))
    verified_name: Mapped[str | None] = mapped_column(String(200))
    subscribed: Mapped[bool] = mapped_column(Boolean, server_default="false")
    registered: Mapped[bool] = mapped_column(Boolean, server_default="false")
    last_error: Mapped[str | None] = mapped_column(String(300))
    operation_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operation_id: Mapped[str | None] = mapped_column(String(40))
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppWebhookReceipt(Record, Base):
    __tablename__ = "whatsapp_webhook_receipts"
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    entry_count: Mapped[int]
