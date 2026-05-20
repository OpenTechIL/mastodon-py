"""S3-backed `Storage`. Async puts via `aiobotocore`.

Configuration matches Mastodon's existing ENV names so existing
production deployments swap in by toggling `S3_ENABLED=true`:

  S3_BUCKET            — bucket name (required)
  S3_REGION            — bucket region (required)
  S3_ENDPOINT          — non-AWS S3 endpoint (e.g. R2, Backblaze, MinIO)
  S3_ALIAS_HOST        — CDN/alias hostname used in public URLs
  S3_ACCESS_KEY_ID     — credentials (or use the boto3 default chain)
  S3_SECRET_ACCESS_KEY — credentials

Public URLs prefer the alias host when set (CDN in front of the bucket),
falling back to a virtual-hosted-style S3 URL otherwise.

A new aiobotocore client is opened per write rather than kept on `self`
because the session is bound to an event loop. Lifetimes get tangled if
the storage instance is reused across test loops, and per-call cost
(~1ms) is negligible compared to the upload itself.
"""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class S3Storage:
    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint_url: str | None = None,
        alias_host: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        self._alias_host = alias_host.rstrip("/") if alias_host else None
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key

    def _client_kwargs(self) -> dict[str, object]:
        kw: dict[str, object] = {"region_name": self._region}
        if self._endpoint_url:
            kw["endpoint_url"] = self._endpoint_url
        if self._access_key_id:
            kw["aws_access_key_id"] = self._access_key_id
        if self._secret_access_key:
            kw["aws_secret_access_key"] = self._secret_access_key
        return kw

    async def read(self, key: str) -> bytes:
        import aiobotocore.session
        from botocore.config import Config

        config = Config(request_checksum_calculation="when_required")
        session = aiobotocore.session.get_session()
        async with session.create_client(
            "s3", config=config, **self._client_kwargs()
        ) as client:
            response = await client.get_object(Bucket=self._bucket, Key=key)
            async with response["Body"] as stream:
                return await stream.read()

    async def write(self, key: str, data: bytes) -> None:
        import aiobotocore.session
        from botocore.config import Config

        content_type, _ = mimetypes.guess_type(key)
        put_kwargs: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
            "ACL": "public-read",
        }
        if content_type:
            put_kwargs["ContentType"] = content_type

        # Newer boto3 defaults send PUT bodies with chunked trailer
        # checksums (`STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER`). Many
        # S3-compatible servers (R2, MinIO, moto) reject the format. Pin
        # `when_required` so we only emit checksums when the operation
        # actually demands them.
        config = Config(request_checksum_calculation="when_required")
        session = aiobotocore.session.get_session()
        async with session.create_client(
            "s3", config=config, **self._client_kwargs()
        ) as client:
            await client.put_object(**put_kwargs)

    def url(self, key: str) -> str:
        if self._alias_host:
            return f"https://{self._alias_host}/{key}"
        # Virtual-hosted-style; mirrors what boto3 generates for a public
        # object. Custom S3-compatible endpoints (R2, MinIO) need an alias
        # host configured — we don't try to compose path-style URLs.
        return f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{key}"
