"""`media_attachments` row.

A single uploaded media file. Created in two phases:

  1. Author uploads → row inserted with `status_id=NULL`. Lives in
     "unattached" state until referenced in a status (or expires).
  2. Author posts a status with `media_ids[<this id>]` → `status_id`
     is set, locking the attachment to that post.

`type` is an integer enum: 0=image, 1=gifv, 2=video, 3=audio, 4=unknown.
This slice only handles `image`; the other branches port with their
respective processors.

`processing` reports the pipeline state to clients polling `GET
/api/v1/media/{id}`: 0=in_progress, 1=ready, 2=failed. We mark uploads
as `ready` immediately because there's no async pipeline yet.

`file_meta` is the Mastodon-shaped JSON blob describing dimensions,
duration, focus point, etc. — clients use it to render aspect ratios
without fetching the asset.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.python.db import Base


class MediaType(IntEnum):
    IMAGE = 0
    GIFV = 1
    VIDEO = 2
    AUDIO = 3
    UNKNOWN = 4

    @property
    def name_for_api(self) -> str:
        return self.name.lower()


class MediaProcessing(IntEnum):
    IN_PROGRESS = 0
    READY = 1
    FAILED = 2


class MediaAttachment(Base):
    __tablename__ = "media_attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("statuses.id"), nullable=True)
    scheduled_status_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    type: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing: Mapped[int | None] = mapped_column(Integer, nullable=True)

    file_file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    file_content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    file_file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    file_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    thumbnail_file_name: Mapped[str | None] = mapped_column(String, nullable=True)
    thumbnail_content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    thumbnail_file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    remote_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    thumbnail_remote_url: Mapped[str | None] = mapped_column(String, nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    blurhash: Mapped[str | None] = mapped_column(String, nullable=True)
    shortcode: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
