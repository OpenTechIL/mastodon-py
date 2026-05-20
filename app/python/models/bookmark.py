"""`bookmarks` join row.

Bookmarks are server-side saves; they're invisible to the bookmarked
account (no notification, no counter on the public status). Only the
owning account ever observes them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class Bookmark(Base):
    __tablename__ = "bookmarks"
    __table_args__ = (UniqueConstraint("account_id", "status_id", name="index_bookmarks_on_account_id_and_status_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    status_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("statuses.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
