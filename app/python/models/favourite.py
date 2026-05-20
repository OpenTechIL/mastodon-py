"""`favourites` join row.

A favourite (`like` in ActivityPub terms) is a unique (account_id,
status_id) pair. The `id` is a snowflake bigint so the legacy admin
"recent activity" views remain sortable when both backends write to the
table during cutover.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class Favourite(Base):
    __tablename__ = "favourites"
    __table_args__ = (
        UniqueConstraint("account_id", "status_id", name="index_favourites_on_account_id_and_status_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    status_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("statuses.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
