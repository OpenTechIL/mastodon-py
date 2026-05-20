"""`custom_filter_statuses` — explicit per-status filter trigger."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class CustomFilterStatus(Base):
    __tablename__ = "custom_filter_statuses"
    __table_args__ = (
        UniqueConstraint(
            "status_id",
            "custom_filter_id",
            name="index_custom_filter_statuses_on_status_id_and_custom_filter_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    custom_filter_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("custom_filters.id"), nullable=False)
    status_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("statuses.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
