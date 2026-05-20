"""`custom_filter_keywords` — a single keyword belonging to a CustomFilter."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.python.db import Base


class CustomFilterKeyword(Base):
    __tablename__ = "custom_filter_keywords"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    custom_filter_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("custom_filters.id"), nullable=False)
    keyword: Mapped[str] = mapped_column(Text, nullable=False, default="")
    whole_word: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
