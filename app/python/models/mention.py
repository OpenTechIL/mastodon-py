"""`mentions` row — a single Account → Status mention link.

`silent` marks a mention that shouldn't appear in the rendered HTML's
`@user` linkification but still applies for visibility / notification
purposes. Mastodon writes a silent mention when a reply is implicit
(e.g. the recipient of a thread reply isn't explicitly typed). We
don't write silent rows in this slice — every parsed mention is loud.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class Mention(Base):
    __tablename__ = "mentions"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "status_id",
            name="index_mentions_on_account_id_and_status_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    status_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("statuses.id"), nullable=False
    )
    silent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
