"""Media-pipeline background jobs.

`prepare_media_attachment` is the async port of Mastodon's
`MediaAttachment::PostProcessing` callback chain. It:

  1. Loads the MediaAttachment row by id.
  2. Reads the original bytes from the configured Storage backend.
  3. Runs the format-specific processor (image / video / audio).
  4. Writes the small variant if applicable.
  5. Updates the row with file_meta, blurhash, file_file_size and
     processing=READY.

Failure path: catches exceptions, marks the row FAILED, swallows the
error. arq won't retry — the client surfaces the failed state on its
next `/api/v1/media/{id}` poll.

The job is also callable inline (the upload path will switch to that
in a follow-up slice). Splitting the work into a `_run` core plus a
context-receiving wrapper lets tests exercise the real logic without
spinning an arq runtime.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.python.db import session_factory
from app.python.models import MediaAttachment, MediaProcessing, MediaType
from app.python.services.media import (
    _detect_type,
    _process_audio,
    _process_image,
    _process_video,
    _small_variant_file_name,
    _storage_key,
)
from app.python.storage import Storage, get_storage

if TYPE_CHECKING:
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncSession


async def _run(session: AsyncSession, storage: Storage, attachment_id: int) -> None:
    """Process one attachment, in-place on the given session.

    Caller owns the transaction — this function only modifies row state
    and storage; it does not commit. Raises nothing: on decode/storage
    failure it flips `processing` to FAILED so the row reflects reality.
    """
    row = (
        await session.execute(select(MediaAttachment).where(MediaAttachment.id == attachment_id))
    ).scalar_one_or_none()
    if row is None:
        return

    if row.file_file_name is None or row.file_content_type is None:
        row.processing = MediaProcessing.FAILED.value
        row.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)
        return

    media_type = _detect_type(row.file_content_type)
    if media_type is MediaType.UNKNOWN:
        row.processing = MediaProcessing.FAILED.value
        row.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)
        return

    original_key = _storage_key(row.id, "original", row.file_file_name)
    try:
        data = await storage.read(original_key)
    except FileNotFoundError:
        row.processing = MediaProcessing.FAILED.value
        row.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)
        return

    if media_type is MediaType.VIDEO:
        processed = await asyncio.to_thread(_process_video, data)
    elif media_type is MediaType.AUDIO:
        processed = await asyncio.to_thread(_process_audio, data)  # type: ignore[arg-type]
    else:
        processed = await asyncio.to_thread(_process_image, data, target_format="PNG")  # type: ignore[arg-type]

    if processed is None:
        row.processing = MediaProcessing.FAILED.value
        row.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)
        return

    original_bytes, original_dims, blurhash_str, small_bytes, small_dims = processed

    # _process_image may re-encode the original (EXIF strip); rewrite to
    # storage when the bytes changed. Identity check is cheap and skips
    # the write for the common passthrough path (video/audio/animated).
    if original_bytes is not data:
        await storage.write(original_key, original_bytes)
        row.file_file_size = len(original_bytes)

    if small_bytes is not None:
        small_name = _small_variant_file_name(media_type, row.file_file_name)
        await storage.write(_storage_key(row.id, "small", small_name), small_bytes)
        if small_name != row.file_file_name:
            row.thumbnail_file_name = small_name
            row.thumbnail_content_type = "image/png"
        row.thumbnail_file_size = len(small_bytes)

    file_meta: dict[str, object] = {"original": original_dims}
    if small_dims is not None:
        file_meta["small"] = small_dims
    row.file_meta = file_meta
    row.blurhash = blurhash_str
    row.processing = MediaProcessing.READY.value
    row.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)


async def prepare_media_attachment(ctx: dict[str, Any], attachment_id: int) -> None:
    """arq entry point. Opens its own session + storage + commits."""
    storage = get_storage()
    async with session_factory()() as session:
        await _run(session, storage, attachment_id)
        await session.commit()
