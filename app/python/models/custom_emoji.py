"""`custom_emojis` row.

A server-side emoji shortcode (e.g. `:partyparrot:`). Local emojis have
their image stored under `/system/custom_emojis/...`; remote ones (from
federated peers we've cached) carry an `image_remote_url`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class CustomEmoji(Base):
    __tablename__ = "custom_emojis"
    __table_args__ = (
        UniqueConstraint(
            "shortcode", "domain", name="index_custom_emojis_on_shortcode_and_domain"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shortcode: Mapped[str] = mapped_column(String, nullable=False, default="")
    domain: Mapped[str | None] = mapped_column(String, nullable=True)

    image_file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    image_content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    image_file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_remote_url: Mapped[str | None] = mapped_column(String, nullable=True)
    image_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    visible_in_picker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uri: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    @property
    def local(self) -> bool:
        return self.domain is None
