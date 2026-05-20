"""`list_accounts` join row.

Connects an `(account_id, list_id)` pair. The optional `follow_id` /
`follow_request_id` columns are denormalisations the legacy backend
maintains so unfollows can cascade-remove list memberships without an
extra lookup; we leave them nullable and don't populate them in this
slice (the cascade behavior ports with the unfollow service later).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class ListAccount(Base):
    __tablename__ = "list_accounts"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "list_id",
            name="index_list_accounts_on_account_id_and_list_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    list_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("lists.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    follow_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    follow_request_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
