"""Storage backends for media files.

A `Storage` writes uploaded bytes under an opaque key and renders the
public URL clients fetch them at. The key convention is Paperclip-style
(`media_attachments/files/<sharded-id>/<variant>/<filename>`) and is
the only domain knowledge baked into callers — backends are otherwise
free to lay out their bytes however they want.

`LocalStorage` writes under `settings.media_root` and serves via the
reverse proxy at `/system/...`. S3/Azure/Swift adapters drop in here
without touching `services/media.py`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from app.python.settings import Settings, get_settings


class Storage(Protocol):
    async def write(self, key: str, data: bytes) -> None: ...
    async def read(self, key: str) -> bytes: ...
    def url(self, key: str) -> str: ...


class LocalStorage:
    """Filesystem-backed storage. Each write creates parent directories
    on demand; that's the same Paperclip behavior callers depend on."""

    def __init__(self, *, root: Path, base_url: str) -> None:
        self._root = root.resolve()
        # Trim trailing slash so we can always join with f"{base}/{key}".
        self._base_url = base_url.rstrip("/")

    async def write(self, key: str, data: bytes) -> None:
        path = self._root / key
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)

    async def read(self, key: str) -> bytes:
        """Raises FileNotFoundError if the key was never written."""
        path = self._root / key
        return await asyncio.to_thread(path.read_bytes)

    def url(self, key: str) -> str:
        return f"{self._base_url}/{key}"


def get_storage(settings: Settings | None = None) -> Storage:
    """Build the storage backend named by settings.

    Not cached — settings can change between tests (the media_root
    tempdir fixture replaces it per-test) and a fresh backend instance
    is essentially free.
    """
    s = settings or get_settings()
    if s.s3_enabled:
        from app.python.storage.s3 import S3Storage

        if not s.s3_bucket or not s.s3_region:
            raise RuntimeError("s3_enabled=true requires s3_bucket and s3_region")
        return S3Storage(
            bucket=s.s3_bucket,
            region=s.s3_region,
            endpoint_url=s.s3_endpoint,
            alias_host=s.s3_alias_host,
            access_key_id=s.s3_access_key_id,
            secret_access_key=s.s3_secret_access_key,
        )
    scheme = "https" if s.env == "production" else "http"
    base_url = f"{scheme}://{s.effective_web_domain}/system"
    return LocalStorage(root=Path(s.media_root), base_url=base_url)
