from sqlalchemy import CheckConstraint, String, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base, Record


class ImportBatch(Record, Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint("total_rows >= 0 AND imported_rows >= 0 AND duplicate_rows >= 0 AND invalid_rows >= 0", name="counts"),
        CheckConstraint("total_rows = imported_rows + duplicate_rows + invalid_rows", name="total"),
    )
    filename: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    total_rows: Mapped[int] = mapped_column(Integer)
    imported_rows: Mapped[int] = mapped_column(Integer)
    duplicate_rows: Mapped[int] = mapped_column(Integer)
    invalid_rows: Mapped[int] = mapped_column(Integer)
    mapping: Mapped[dict] = mapped_column(JSONB)
    country: Mapped[str] = mapped_column(String(2))
    issues: Mapped[list] = mapped_column(JSONB, server_default="[]")
