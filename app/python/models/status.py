"""`statuses` row — read-side columns.

Write-side columns (`local`, `reply`, `trendable`, `fetched_replies_at`,
`ordered_media_attachment_ids`) are present in the table but only read
indirectly by this slice; they get used when Phase 3 adds posting.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.python.common.discard import Discardable
from app.python.db import Base

if TYPE_CHECKING:
    from app.python.models.account import Account
    from app.python.models.status_stat import StatusStat


class Visibility(IntEnum):
    PUBLIC = 0
    UNLISTED = 1
    PRIVATE = 2
    DIRECT = 3
    LIMITED = 4

    @property
    def name_for_api(self) -> str:
        # `limited` masks as `private` upstream so clients have no extra UX state.
        return "private" if self is Visibility.LIMITED else self.name.lower()


class Status(Base, Discardable):
    __tablename__ = "statuses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    spoiler_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    visibility: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str | None] = mapped_column(String, nullable=True)
    local: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    in_reply_to_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    in_reply_to_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reblog_of_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("statuses.id"), nullable=True)
    application_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    uri: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)

    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    account: Mapped[Account] = relationship(
        "Account",
        primaryjoin="Status.account_id == Account.id",
        lazy="joined",
        viewonly=True,
    )

    reblog: Mapped[Status | None] = relationship(
        "Status",
        primaryjoin="Status.reblog_of_id == Status.id",
        remote_side="Status.id",
        lazy="joined",
        join_depth=1,  # self-referential joined loads don't recurse without this
        viewonly=True,
        uselist=False,
    )

    stat: Mapped[StatusStat | None] = relationship(
        "StatusStat",
        primaryjoin="Status.id == StatusStat.status_id",
        uselist=False,
        lazy="joined",
        viewonly=True,
    )

    @property
    def visibility_enum(self) -> Visibility:
        return Visibility(self.visibility)

    @property
    def is_public(self) -> bool:
        return self.visibility_enum in (Visibility.PUBLIC, Visibility.UNLISTED)

    @property
    def is_reblog(self) -> bool:
        return self.reblog_of_id is not None
