"""`accounts` row — columns the REST + ActivityPub read paths need."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.python.db import Base

if TYPE_CHECKING:
    from app.python.models.account_stat import AccountStat
    from app.python.models.user import User


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False, default="")
    domain: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discoverable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    indexable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    memorial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hide_collections: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    actor_type: Mapped[str | None] = mapped_column(String, nullable=True)

    uri: Mapped[str] = mapped_column(String, nullable=False, default="")
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    # ActivityPub actor public key (PEM). Always populated on remote
    # actors once we've ingested them; on local actors it's set at
    # account creation and stays stable.
    public_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Private key (PEM). Local actors only — remote rows carry an
    # empty string. Used by `sign_and_deliver` to sign outbound POSTs.
    private_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Inbox the remote server expects deliveries at. Empty on local
    # actors; for them, we serve our own inbox endpoints.
    inbox_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Optional batched-delivery endpoint (one POST for many recipients
    # on the same domain). Mastodon advertises this on shared hosts.
    shared_inbox_url: Mapped[str] = mapped_column(String, nullable=False, default="")

    avatar_file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_remote_url: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_content_type: Mapped[str | None] = mapped_column(String, nullable=True)

    header_file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    header_remote_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    header_content_type: Mapped[str | None] = mapped_column(String, nullable=True)

    moved_to_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    silenced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    suspension_origin: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sensitized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    fields: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    user: Mapped[User | None] = relationship(  # type: ignore[name-defined]
        "User",
        back_populates="account",
        uselist=False,
        lazy="joined",
    )

    stat: Mapped[AccountStat | None] = relationship(  # type: ignore[name-defined]
        "AccountStat",
        primaryjoin="Account.id == AccountStat.account_id",
        uselist=False,
        lazy="joined",
        viewonly=True,
    )

    @property
    def local(self) -> bool:
        return self.domain is None

    @property
    def acct(self) -> str:
        return self.username if self.local else f"{self.username}@{self.domain}"

    @property
    def bot(self) -> bool:
        # Rails: actor_type in ('Application', 'Service') ⇒ bot=true
        return self.actor_type in ("Application", "Service")

    @property
    def group(self) -> bool:
        return self.actor_type == "Group"

    @property
    def suspended(self) -> bool:
        return self.suspended_at is not None

    @property
    def silenced(self) -> bool:
        return self.silenced_at is not None

    @property
    def sensitized(self) -> bool:
        return self.sensitized_at is not None
