"""`custom_filters` row.

`action` is an integer enum:
  0 = warn   — client renders a "click to reveal" interstitial
  1 = hide   — client should not display the post at all

`context` is a string array of which timelines the filter applies to:
  home, notifications, public, thread, account.

`expires_at` is optional; an absent value means the filter never expires.
The legacy backend sweeps expired filters in a periodic job; until that
ports, the application engine here just checks `expires_at` on every
read.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from sqlalchemy import ARRAY, BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.python.db import Base


class FilterAction(IntEnum):
    WARN = 0
    HIDE = 1

    @property
    def name_for_api(self) -> str:
        return self.name.lower()


_ACTION_BY_NAME = {a.name_for_api: a for a in FilterAction}


def parse_filter_action(value: str | None) -> FilterAction:
    if value is None:
        return FilterAction.WARN
    try:
        return _ACTION_BY_NAME[value]
    except KeyError as exc:
        raise ValueError(f"invalid filter_action: {value!r}") from exc


VALID_CONTEXTS = frozenset({"home", "notifications", "public", "thread", "account"})


class CustomFilter(Base):
    __tablename__ = "custom_filters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    action: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context: Mapped[list[str]] = mapped_column(
        ARRAY(String).with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    phrase: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
