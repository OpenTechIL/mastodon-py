"""`follows` row.

A directed edge from `account_id` to `target_account_id`. The per-follow
settings (`show_reblogs`, `notify`, `languages`) modulate how the
target's posts appear in the follower's home feed; the visibility-policy
check only consults the row's existence.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.python.db import Base


class Follow(Base):
    __tablename__ = "follows"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "target_account_id",
            name="index_follows_on_account_id_and_target_account_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    target_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    show_reblogs: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    languages: Mapped[list[str] | None] = mapped_column(
        ARRAY(String).with_variant(JSON(), "sqlite"),
        nullable=True,
    )
    uri: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
