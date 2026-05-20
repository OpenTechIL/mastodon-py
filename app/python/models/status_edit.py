"""`status_edits` row — snapshot of a status at a prior revision.

Created before each `UpdateStatusService` write so the user can audit
what changed and clients can render a per-edit history view.

The media / poll / quote columns are present in the schema; this slice
writes them as `None` / empty arrays because the underlying features
aren't yet modeled. The columns must still be declared because both
backends share the table during cutover.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ARRAY, BigInteger, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.python.db import Base

_PG_OR_JSON_TEXT_ARRAY = ARRAY(Text).with_variant(JSON(), "sqlite")
_PG_OR_JSON_BIGINT_ARRAY = ARRAY(BigInteger).with_variant(JSON(), "sqlite")


class StatusEdit(Base):
    __tablename__ = "status_edits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("statuses.id"), nullable=False)
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    spoiler_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sensitive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    ordered_media_attachment_ids: Mapped[list[int] | None] = mapped_column(_PG_OR_JSON_BIGINT_ARRAY, nullable=True)
    media_descriptions: Mapped[list[str] | None] = mapped_column(_PG_OR_JSON_TEXT_ARRAY, nullable=True)
    poll_options: Mapped[list[str] | None] = mapped_column(ARRAY(String).with_variant(JSON(), "sqlite"), nullable=True)
    quote_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
