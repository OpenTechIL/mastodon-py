"""`account_pins` — an endorsement (account "featured" on a profile).

Distinct from `StatusPin` (which pins a status). An account pin
publicly highlights another account on the pinner's profile page.
Requires a pre-existing Follow — Mastodon refuses to pin someone you
don't follow.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class AccountPin(Base):
    __tablename__ = "account_pins"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "target_account_id",
            name="index_account_pins_on_account_id_and_target_account_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    target_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
