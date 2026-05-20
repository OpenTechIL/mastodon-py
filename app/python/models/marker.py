"""`markers` row — per-user, per-timeline read position.

`lock_version` increments on every write; clients can pass it back to
detect conflicts (we don't enforce conflict yet; the slice just writes
through and bumps the column).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class Marker(Base):
    __tablename__ = "markers"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "timeline", name="index_markers_on_user_id_and_timeline"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    timeline: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_read_id: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
