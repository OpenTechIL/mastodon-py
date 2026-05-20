"""`account_notes` row.

A private note one account keeps about another. Visible only to the
note's author; surfaces in the `note` field of the Relationship shape.
Mastodon's UI displays it on the target's profile as a sticky reminder
("met at conference 2024", etc.).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class AccountNote(Base):
    __tablename__ = "account_notes"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "target_account_id",
            name="index_account_notes_on_account_id_and_target_account_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    target_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
