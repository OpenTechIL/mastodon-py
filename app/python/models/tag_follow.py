"""`tag_follows` row — an account follows a hashtag."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class TagFollow(Base):
    __tablename__ = "tag_follows"
    __table_args__ = (UniqueConstraint("account_id", "tag_id", name="index_tag_follows_on_account_id_and_tag_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("accounts.id"), nullable=False)
    tag_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tags.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
