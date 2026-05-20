"""Tests for the S3 storage adapter.

We don't run a real S3 emulator — moto's HTTP-level mocks don't play
well with aiobotocore's response handling and the integration is
brittle. Instead we patch `aiobotocore.session.get_session` to return
a fake session whose `create_client` returns a recorder. That tests
the actual contract we care about: what kwargs we send to `put_object`
for each upload.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.python.storage import get_storage
from app.python.storage.s3 import S3Storage


@dataclass
class _PutCall:
    Bucket: str
    Key: str
    Body: bytes
    ACL: str | None = None
    ContentType: str | None = None


@dataclass
class _FakeClient:
    calls: list[_PutCall] = field(default_factory=list)

    async def put_object(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(
            _PutCall(
                Bucket=kwargs["Bucket"],
                Key=kwargs["Key"],
                Body=kwargs["Body"],
                ACL=kwargs.get("ACL"),
                ContentType=kwargs.get("ContentType"),
            )
        )
        return {"ETag": '"fake"'}


@dataclass
class _FakeSession:
    client: _FakeClient = field(default_factory=_FakeClient)
    create_client_kwargs: dict[str, Any] = field(default_factory=dict)

    def create_client(self, service_name: str, **kwargs: Any):
        # Stash the kwargs for assertions; aiobotocore's API hands them
        # through to the underlying botocore client.
        self.create_client_kwargs = {"service_name": service_name, **kwargs}
        client = self.client

        @asynccontextmanager
        async def _ctx():
            yield client

        return _ctx()


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    session = _FakeSession()
    import aiobotocore.session

    monkeypatch.setattr(
        aiobotocore.session, "get_session", lambda: session
    )
    return session


@pytest.mark.asyncio
async def test_write_calls_put_object_with_bucket_key_body(
    fake_session: _FakeSession,
) -> None:
    storage = S3Storage(bucket="b", region="us-east-1")
    await storage.write("foo/bar.png", b"\x89PNG fake")

    [call] = fake_session.client.calls
    assert call.Bucket == "b"
    assert call.Key == "foo/bar.png"
    assert call.Body == b"\x89PNG fake"


@pytest.mark.asyncio
async def test_write_sets_content_type_from_key_extension(
    fake_session: _FakeSession,
) -> None:
    storage = S3Storage(bucket="b", region="us-east-1")
    await storage.write("clip.mp4", b"x")
    await storage.write("p.jpg", b"y")
    await storage.write("no-extension", b"z")

    types = [c.ContentType for c in fake_session.client.calls]
    assert types[0] == "video/mp4"
    assert types[1] == "image/jpeg"
    assert types[2] is None  # mimetypes guesses nothing → omit the header


@pytest.mark.asyncio
async def test_write_uses_public_read_acl(
    fake_session: _FakeSession,
) -> None:
    """Mastodon media is fetched anonymously; ACL must be public-read."""
    storage = S3Storage(bucket="b", region="us-east-1")
    await storage.write("a.txt", b"x")
    assert fake_session.client.calls[0].ACL == "public-read"


@pytest.mark.asyncio
async def test_write_passes_endpoint_and_credentials_to_client(
    fake_session: _FakeSession,
) -> None:
    """Custom endpoint + creds reach `create_client` for non-AWS S3
    (Cloudflare R2, MinIO, Backblaze)."""
    storage = S3Storage(
        bucket="b",
        region="us-east-1",
        endpoint_url="https://r2.example.test",
        access_key_id="AKIAFAKE",
        secret_access_key="secretfake",
    )
    await storage.write("a.txt", b"x")

    kw = fake_session.create_client_kwargs
    assert kw["endpoint_url"] == "https://r2.example.test"
    assert kw["aws_access_key_id"] == "AKIAFAKE"
    assert kw["aws_secret_access_key"] == "secretfake"


def test_url_prefers_alias_host_when_set() -> None:
    s = S3Storage(
        bucket="b", region="us-west-2", alias_host="cdn.example.test"
    )
    assert s.url("x/y.png") == "https://cdn.example.test/x/y.png"


def test_url_falls_back_to_virtual_hosted_s3() -> None:
    s = S3Storage(bucket="bucket1", region="eu-west-1")
    assert s.url("a/b.jpg") == "https://bucket1.s3.eu-west-1.amazonaws.com/a/b.jpg"


def test_alias_host_trailing_slash_is_trimmed() -> None:
    s = S3Storage(bucket="b", region="us-east-1", alias_host="cdn.example.test/")
    assert s.url("a.png") == "https://cdn.example.test/a.png"


def test_get_storage_dispatches_to_s3_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.python.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("S3_ENABLED", "true")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    monkeypatch.setenv("S3_ALIAS_HOST", "cdn.example.test")
    try:
        backend = get_storage()
        assert isinstance(backend, S3Storage)
        assert backend.url("k.png") == "https://cdn.example.test/k.png"
    finally:
        get_settings.cache_clear()


def test_get_storage_raises_when_s3_enabled_but_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.python.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("S3_ENABLED", "true")
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.delenv("S3_REGION", raising=False)
    try:
        with pytest.raises(RuntimeError, match="s3_bucket"):
            get_storage()
    finally:
        get_settings.cache_clear()
