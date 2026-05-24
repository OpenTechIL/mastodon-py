"""Tests for the `prepare_media_attachment` worker.

We invoke `_run` directly with a test session + LocalStorage rather
than spinning an arq runtime. The job's contract is "read the original
from storage, run the processor, write the small, flip processing to
READY (or FAILED)" — we test exactly that.
"""

from __future__ import annotations

import io
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import MediaAttachment, MediaProcessing, MediaType
from app.python.services.media import _storage_key
from app.python.storage import LocalStorage
from app.python.workers.media import _run


def _make_png(width: int = 32, height: int = 32) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (0, 200, 100)).save(buf, format="PNG")
    return buf.getvalue()


async def _insert_pending_attachment(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    file_name: str,
    content_type: str,
    file_size: int,
) -> int:
    """Seed an alice + a PROCESSING-state attachment owned by her."""
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_user"](id_=1, account_id=1),
            ]
        )
        row = MediaAttachment(
            id=987654321,
            account_id=1,
            status_id=None,
            scheduled_status_id=None,
            type=MediaType.IMAGE.value,
            processing=MediaProcessing.IN_PROGRESS.value,
            file_file_name=file_name,
            file_content_type=content_type,
            file_file_size=file_size,
            file_meta=None,
            file_updated_at=now,
            thumbnail_file_name=None,
            thumbnail_content_type=None,
            thumbnail_file_size=None,
            remote_url="",
            thumbnail_remote_url=None,
            description=None,
            blurhash=None,
            shortcode=None,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        await s.commit()
        return row.id


@pytest.mark.asyncio
async def test_run_flips_to_ready_and_writes_small(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    png = _make_png(width=200, height=100)
    with tempfile.TemporaryDirectory() as d:
        storage = LocalStorage(root=Path(d), base_url="https://example.test/system")
        aid = await _insert_pending_attachment(
            session_factory,
            seed_data,
            file_name="img.png",
            content_type="image/png",
            file_size=len(png),
        )
        await storage.write(_storage_key(aid, "original", "img.png"), png)

        async with session_factory() as s:
            await _run(s, storage, aid)
            await s.commit()

        async with session_factory() as s:
            row = (
                await s.execute(
                    select(MediaAttachment).where(MediaAttachment.id == aid)
                )
            ).scalar_one()
            assert row.processing == MediaProcessing.READY.value
            assert row.blurhash
            assert row.file_meta is not None
            assert row.file_meta["original"]["width"] == 200
            assert row.file_meta["small"]["width"] <= 400
        # Small variant exists on disk under the same root.
        assert (Path(d) / _storage_key(aid, "small", "img.png")).is_file()


@pytest.mark.asyncio
async def test_run_marks_failed_when_original_missing(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    with tempfile.TemporaryDirectory() as d:
        storage = LocalStorage(root=Path(d), base_url="https://example.test/system")
        aid = await _insert_pending_attachment(
            session_factory,
            seed_data,
            file_name="lost.png",
            content_type="image/png",
            file_size=42,
        )
        # No bytes written to storage — read will raise FileNotFoundError.

        async with session_factory() as s:
            await _run(s, storage, aid)
            await s.commit()

        async with session_factory() as s:
            row = (
                await s.execute(
                    select(MediaAttachment).where(MediaAttachment.id == aid)
                )
            ).scalar_one()
            assert row.processing == MediaProcessing.FAILED.value


@pytest.mark.asyncio
async def test_run_marks_failed_when_bytes_are_garbage(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    with tempfile.TemporaryDirectory() as d:
        storage = LocalStorage(root=Path(d), base_url="https://example.test/system")
        aid = await _insert_pending_attachment(
            session_factory,
            seed_data,
            file_name="x.png",
            content_type="image/png",
            file_size=4,
        )
        await storage.write(_storage_key(aid, "original", "x.png"), b"junk")

        async with session_factory() as s:
            await _run(s, storage, aid)
            await s.commit()

        async with session_factory() as s:
            row = (
                await s.execute(
                    select(MediaAttachment).where(MediaAttachment.id == aid)
                )
            ).scalar_one()
            assert row.processing == MediaProcessing.FAILED.value


@pytest.mark.asyncio
async def test_run_skips_missing_row(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A job firing after the row has been deleted is a no-op."""
    with tempfile.TemporaryDirectory() as d:
        storage = LocalStorage(root=Path(d), base_url="https://example.test/system")
        async with session_factory() as s:
            await _run(s, storage, attachment_id=99999999)
            await s.commit()
