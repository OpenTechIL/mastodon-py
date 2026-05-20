"""`account_stats` — counter cache for accounts.

Counts here are denormalized; the source of truth lives in the
`statuses`, `follows`, etc. tables. Phase 3 will introduce a write path
that updates these rows explicitly (via `common.counter_cache`).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class AccountStat(Base):
    __tablename__ = "account_stats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False, unique=True)
    statuses_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    following_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    followers_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
