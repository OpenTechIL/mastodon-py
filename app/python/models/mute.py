"""`mutes` row.

One-directional: only the muter is affected. The target's posts and
optionally notifications are hidden from the muter; the target sees no
change. Optional `expires_at` for temporary mutes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class Mute(Base):
    __tablename__ = "mutes"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "target_account_id",
            name="index_mutes_on_account_id_and_target_account_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    target_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    hide_notifications: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
