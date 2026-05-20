"""`users` row — local-account credential record.

A `User` exists only for local accounts; remote (federated) accounts live
solely in `accounts`. Token-based auth ultimately yields a
`(User, Account)` pair, with `User` carrying the password hash, OTP
secret, and admin-side flags (`disabled`, `approved`, `confirmed_at`)
that gate whether the token is usable.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.python.db import Base


class _StringList(TypeDecorator):
    """varchar[] on Postgres, JSON-encoded text on SQLite (for tests)."""
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import ARRAY
            return dialect.type_descriptor(ARRAY(String))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if dialect.name == "postgresql":
            return value
        if value is None:
            return None
        import json
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if dialect.name == "postgresql":
            return value
        if value is None:
            return None
        import json
        return json.loads(value)


if TYPE_CHECKING:
    from app.python.models.account import Account


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )

    email: Mapped[str] = mapped_column(String, nullable=False, default="")
    encrypted_password: Mapped[str] = mapped_column(String, nullable=False, default="")

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    otp_required_for_login: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    otp_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    consumed_timestep: Mapped[int | None] = mapped_column(Integer, nullable=True)

    role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    locale: Mapped[str | None] = mapped_column(String, nullable=True)
    chosen_languages: Mapped[list[str] | None] = mapped_column(_StringList, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    account: Mapped["Account"] = relationship(  # type: ignore[name-defined]
        "Account",
        back_populates="user",
        uselist=False,
        lazy="joined",
    )

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None

    @property
    def functional(self) -> bool:
        """A user whose token should be honoured for API calls."""
        return self.confirmed and self.approved and not self.disabled

    @property
    def otp_enabled(self) -> bool:
        return self.otp_required_for_login and bool(self.otp_secret)
