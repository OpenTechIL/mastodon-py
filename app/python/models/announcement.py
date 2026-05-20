"""`announcements` row.

A server-wide notice that appears at the top of the timeline. Only
`published=true` announcements surface via the API; admins can stage
drafts. The `status_ids` array is an admin-attached list of statuses
"this announcement embeds"; we keep the column but don't dereference it
yet — the embedded-Status serializer dependency would pull in the whole
content rendering pipeline.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, BigInteger, Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.python.db import Base


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    notification_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False)
    )
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status_ids: Mapped[list[int] | None] = mapped_column(
        ARRAY(BigInteger).with_variant(JSON(), "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
