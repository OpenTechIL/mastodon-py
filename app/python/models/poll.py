"""`polls` row — choose-one or choose-many attached to a Status.

`options` is an ordered array of option titles. `cached_tallies` is a
parallel array of vote counts; index N in tallies maps to option N in
options. `votes_count` is the total vote count across all options.
`voters_count` is the count of distinct accounts that have voted —
populated only for `multiple=true` polls (otherwise it equals
`votes_count`).

`hide_totals` is meaningful only while the poll is open; closed polls
always expose totals. `lock_version` tracks optimistic concurrency on
the tally bumps (deferred; we update under a transaction).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import ARRAY, BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.python.db import Base

_PG_OR_JSON_STR_ARRAY = ARRAY(String).with_variant(JSON(), "sqlite")
_PG_OR_JSON_BIGINT_ARRAY = ARRAY(BigInteger).with_variant(JSON(), "sqlite")


class Poll(Base):
    __tablename__ = "polls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    status_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("statuses.id"), nullable=False)

    options: Mapped[list[str]] = mapped_column(_PG_OR_JSON_STR_ARRAY, nullable=False, default=list)
    cached_tallies: Mapped[list[int]] = mapped_column(_PG_OR_JSON_BIGINT_ARRAY, nullable=False, default=list)

    multiple: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hide_totals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    votes_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    voters_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(tz=UTC)
