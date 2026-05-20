"""`poll_votes` — one row per (account, choice) pair on a Poll.

Single-choice polls produce one row per account. Multiple-choice polls
can produce up to `len(poll.options)` rows per account (one per chosen
option). No unique constraint at the schema level — the service
enforces idempotence and choice-set semantics.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class PollVote(Base):
    __tablename__ = "poll_votes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    poll_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("polls.id"), nullable=False
    )
    choice: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uri: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
