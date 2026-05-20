"""`reports` row — a moderation report filed by a user.

The reporter is `account_id`; the reported account is `target_account_id`.
`status_ids` is an array of specific statuses the reporter wants the
moderator to review; `rule_ids` is an array of server rules the
reporter is citing.

`action_taken_at` / `action_taken_by_account_id` are filled in by
moderators via the admin API — we declare them so the row is
schema-compatible, but the user-side endpoint only writes nulls.

`category` is an integer enum:
  0 = other
  1 = spam
  2 = legal (newer; varies by deployment)
  3 = violation
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from sqlalchemy import ARRAY, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.python.db import Base


class ReportCategory(IntEnum):
    OTHER = 0
    SPAM = 1
    LEGAL = 2
    VIOLATION = 3

    @property
    def name_for_api(self) -> str:
        return self.name.lower()


_CATEGORY_BY_NAME = {c.name_for_api: c for c in ReportCategory}


def parse_report_category(value: str | None) -> ReportCategory:
    if value is None:
        return ReportCategory.OTHER
    try:
        return _CATEGORY_BY_NAME[value]
    except KeyError as exc:
        raise ValueError(f"invalid report category: {value!r}") from exc


_PG_OR_JSON_BIGINT_ARRAY = ARRAY(BigInteger).with_variant(JSON(), "sqlite")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    target_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)

    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status_ids: Mapped[list[int]] = mapped_column(_PG_OR_JSON_BIGINT_ARRAY, nullable=False, default=list)
    rule_ids: Mapped[list[int] | None] = mapped_column(_PG_OR_JSON_BIGINT_ARRAY, nullable=True)
    forwarded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    uri: Mapped[str | None] = mapped_column(String, nullable=True)

    application_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    assigned_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action_taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    action_taken_by_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
