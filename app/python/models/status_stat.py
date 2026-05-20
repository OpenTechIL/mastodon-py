"""`status_stats` — counter cache for statuses."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class StatusStat(Base):
    __tablename__ = "status_stats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("statuses.id"), nullable=False, unique=True
    )
    replies_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reblogs_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    favourites_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    quotes_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    untrusted_reblogs_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    untrusted_favourites_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
