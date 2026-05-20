"""`blocks` row.

A hard cut between two accounts. The blocker can't see the target's
content and vice-versa; neither side can follow the other while the
block exists.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "target_account_id",
            name="index_blocks_on_account_id_and_target_account_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    target_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    uri: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
