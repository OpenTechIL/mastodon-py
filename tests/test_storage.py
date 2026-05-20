"""Unit tests for the storage adapter."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.python.storage import LocalStorage, get_storage


@pytest.mark.asyncio
async def test_local_storage_writes_under_root() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = LocalStorage(root=Path(d), base_url="https://example.test/system")
        await s.write("foo/bar/baz.txt", b"hello")
        assert (Path(d) / "foo/bar/baz.txt").read_bytes() == b"hello"


@pytest.mark.asyncio
async def test_local_storage_round_trips_bytes() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = LocalStorage(root=Path(d), base_url="https://example.test/system")
        await s.write("a/b.bin", b"\x00\x01\x02")
        assert await s.read("a/b.bin") == b"\x00\x01\x02"


@pytest.mark.asyncio
async def test_local_storage_read_missing_raises() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = LocalStorage(root=Path(d), base_url="https://example.test/system")
        with pytest.raises(FileNotFoundError):
            await s.read("never/written.bin")


@pytest.mark.asyncio
async def test_local_storage_creates_parent_dirs() -> None:
    """Deep keys land in nested directories the caller never created."""
    with tempfile.TemporaryDirectory() as d:
        s = LocalStorage(root=Path(d), base_url="https://example.test/system")
        deep = "a/b/c/d/e/f.bin"
        await s.write(deep, b"x")
        assert (Path(d) / deep).is_file()


def test_local_storage_url_joins_cleanly() -> None:
    s = LocalStorage(root=Path("/tmp"), base_url="https://example.test/system")
    assert s.url("x/y/z.png") == "https://example.test/system/x/y/z.png"


def test_local_storage_url_trims_trailing_slash() -> None:
    """A base URL with a trailing slash shouldn't double up."""
    s = LocalStorage(root=Path("/tmp"), base_url="https://example.test/system/")
    assert s.url("a.png") == "https://example.test/system/a.png"


def test_get_storage_reads_current_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory must pick up MEDIA_ROOT changes — tests rely on it for
    per-test tempdirs without restarting the process."""
    from app.python.settings import get_settings

    with tempfile.TemporaryDirectory() as d:
        get_settings.cache_clear()
        monkeypatch.setenv("MEDIA_ROOT", d)
        s = get_storage()
        # Round-trip through asset URL composition: the key should land
        # under d on disk if we wrote to it.
        assert isinstance(s, LocalStorage)
        assert str(s._root) == str(Path(d).resolve())  # type: ignore[attr-defined]
    get_settings.cache_clear()
