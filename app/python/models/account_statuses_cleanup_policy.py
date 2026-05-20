"""`account_statuses_cleanup_policies` — auto-deletion settings per account."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class AccountStatusesCleanupPolicy(Base):
    __tablename__ = "account_statuses_cleanup_policies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False, unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    min_status_age: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1_209_600  # 2 weeks in seconds
    )
    keep_direct: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    keep_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    keep_polls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keep_media: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    keep_self_fav: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    keep_self_bookmark: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    min_favs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_reblogs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
