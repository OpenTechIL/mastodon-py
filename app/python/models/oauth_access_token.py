"""`oauth_access_tokens` row."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.python.db import Base

if TYPE_CHECKING:
    from app.python.models.oauth_application import OAuthApplication
    from app.python.models.user import User


class OAuthAccessToken(Base):
    __tablename__ = "oauth_access_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(String, nullable=True)
    scopes: Mapped[str | None] = mapped_column(String, nullable=True)

    application_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resource_owner_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    expires_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_used_ip: Mapped[str | None] = mapped_column(String, nullable=True)

    application: Mapped["OAuthApplication | None"] = relationship(  # type: ignore[name-defined]
        "OAuthApplication",
        primaryjoin="OAuthAccessToken.application_id == OAuthApplication.id",
        foreign_keys=lambda: [OAuthAccessToken.application_id],
        lazy="joined",
        viewonly=True,
    )
    user: Mapped["User | None"] = relationship(  # type: ignore[name-defined]
        "User",
        primaryjoin="OAuthAccessToken.resource_owner_id == User.id",
        foreign_keys=lambda: [OAuthAccessToken.resource_owner_id],
        lazy="joined",
        viewonly=True,
    )

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_in is None:
            return False
        now = now or datetime.now(tz=timezone.utc)
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created + timedelta(seconds=self.expires_in) <= now

    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def scope_list(self) -> set[str]:
        return set((self.scopes or "").split())
