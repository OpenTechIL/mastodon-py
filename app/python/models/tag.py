"""`tags` row.

`name` is the lowercase canonical form (used for lookups); `display_name`
preserves the original casing as posted (used for rendering). The
schema's unique index is on `lower(name)` so case-insensitive lookups
hit the index in production; we just normalize on insert.

`listable` / `usable` / `trendable` are moderation flags. The trending
pipeline owns those; this slice writes the defaults and reads them but
doesn't move them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)

    usable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    listable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    trendable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    requested_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_score_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
