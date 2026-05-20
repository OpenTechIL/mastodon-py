"""`lists` row — a user-curated subset of their follows.

`replies_policy` is an integer enum:
  0 = followed       (default; show replies to anyone the list owner follows)
  1 = list           (show replies only between list members)
  2 = none           (hide all replies)

`exclusive` removes list members from the owner's home timeline so the
list is the only place those accounts surface.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class RepliesPolicy(IntEnum):
    FOLLOWED = 0
    LIST = 1
    NONE = 2

    @property
    def name_for_api(self) -> str:
        return self.name.lower()


_REPLIES_BY_NAME = {p.name_for_api: p for p in RepliesPolicy}


def parse_replies_policy(value: str | None) -> RepliesPolicy:
    if value is None:
        return RepliesPolicy.FOLLOWED
    try:
        return _REPLIES_BY_NAME[value]
    except KeyError as exc:
        raise ValueError(f"invalid replies_policy: {value!r}") from exc


class List(Base):
    __tablename__ = "lists"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    replies_policy: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
