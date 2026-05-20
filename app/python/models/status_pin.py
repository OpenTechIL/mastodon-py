"""`status_pins` join row.

A status pin makes the author's own status appear above their other
posts on the profile timeline. Mastodon caps the count at five per
account; the service enforces that, not the schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class StatusPin(Base):
    __tablename__ = "status_pins"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "status_id",
            name="index_status_pins_on_account_id_and_status_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("accounts.id"), nullable=False
    )
    status_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("statuses.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
