"""`oauth_applications` row."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class OAuthApplication(Base):
    __tablename__ = "oauth_applications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    uid: Mapped[str] = mapped_column(String, nullable=False)
    secret: Mapped[str] = mapped_column(String, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(String, nullable=False, default="")
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    confidential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    superapp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_type: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    @property
    def redirect_uris(self) -> list[str]:
        """The Mastodon API returns redirect_uri as a list since 4.3."""
        return [uri for uri in self.redirect_uri.split("\n") if uri.strip()]
