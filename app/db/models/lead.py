from uuid import UUID
from sqlalchemy import CheckConstraint, String, Text, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, MutableRecord


class Lead(MutableRecord, Base):
    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'contacted', 'qualified', 'unqualified', 'closed')",
            name="status",
        ),
    )
    name: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(100))
    preferred_language: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), server_default="new", index=True)

    email: Mapped[str | None] = mapped_column(String(254), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    extra_fields: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    import_batch_id: Mapped[UUID | None] = mapped_column(ForeignKey("app.import_batches.id"), index=True)
