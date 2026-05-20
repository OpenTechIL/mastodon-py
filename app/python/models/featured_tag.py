"""`featured_tags` — an account's curated hashtag pin.

Mastodon shows these as quick-access chips on a profile. Counts and
last-status timestamps are denormalised; the legacy backend keeps them
in sync via callbacks on Status create/destroy. Our slice writes the
initial values on create; cross-write maintenance lands when the post-
status service grows hashtag-driven side effects (alongside the
trending pipeline).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class FeaturedTag(Base):
    __tablename__ = "featured_tags"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "tag_id",
            name="index_featured_tags_on_account_id_and_tag_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    tag_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tags.id"), nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    statuses_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
