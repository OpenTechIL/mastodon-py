"""`announcement_mutes` — the "I've read this" marker.

Mastodon's name is `mute` for historical reasons; the v2 API exposes it
as the `read` boolean on Announcement. A row exists iff the viewer has
dismissed the announcement.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class AnnouncementMute(Base):
    __tablename__ = "announcement_mutes"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "announcement_id",
            name="index_announcement_mutes_on_account_id_and_announcement_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    announcement_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("announcements.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
