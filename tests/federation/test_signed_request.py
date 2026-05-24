"""End-to-end tests for `verify_signed_request`.

These exercise the full inbound path: signed by a real RSA key,
key resolved from the DB (local) or via respx-mocked HTTP (remote),
signature verified, actor URL returned.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.federation.signatures import sign_request
from app.python.federation.signed_request import verify_signed_request


def _make_keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, bytes]:
    return _make_keypair()


def _sign_post(
    priv: bytes, *, actor_url: str, host: str, path: str, body: bytes
) -> dict[str, str]:
    headers: dict[str, str] = {
        "Host": host,
        "Content-Type": "application/activity+json",
    }
    sign_request(
        method="POST",
        path=path,
        headers=headers,
        body=body,
        key_id=f"{actor_url}#main-key",
        private_key_pem=priv,
    )
    return headers


@pytest.mark.asyncio
async def test_verify_with_local_account_returns_actor_url(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = "https://example.test/users/alice"
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=100,
                username="alice",
                domain="example.test",
                uri=actor_url,
                public_key=pub.decode("utf-8"),
            )
        )
        s.add(seed_data["make_account_stat"](account_id=100))
        await s.commit()

    body = b'{"@context":"https://www.w3.org/ns/activitystreams","type":"Create"}'
    headers = _sign_post(
        priv, actor_url=actor_url, host="example.test", path="/inbox", body=body
    )

    async with session_factory() as s:
        async with httpx.AsyncClient() as client:
            verified = await verify_signed_request(
                method="POST",
                path="/inbox",
                headers=headers,
                body=body,
                session=s,
                http_client=client,
            )
    assert verified == actor_url


@pytest.mark.asyncio
async def test_verify_with_remote_actor_fetches_key(
    session_factory: async_sessionmaker[AsyncSession],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = "https://other.test/users/bob"
    key_id = f"{actor_url}#main-key"
    actor_json = {
        "id": actor_url,
        "publicKey": {"id": key_id, "publicKeyPem": pub.decode()},
    }

    body = b'{"type":"Follow"}'
    headers = _sign_post(
        priv, actor_url=actor_url, host="other.test", path="/inbox", body=body
    )

    async with respx.mock() as router:
        router.get(actor_url).respond(json=actor_json)
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                verified = await verify_signed_request(
                    method="POST",
                    path="/inbox",
                    headers=headers,
                    body=body,
                    session=s,
                    http_client=client,
                )
    assert verified == actor_url


@pytest.mark.asyncio
async def test_verify_rejects_tampered_body(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    actor_url = "https://example.test/users/alice"
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=101, username="alice", domain="example.test",
                uri=actor_url, public_key=pub.decode("utf-8"),
            )
        )
        s.add(seed_data["make_account_stat"](account_id=101))
        await s.commit()

    body = b'{"type":"Create"}'
    headers = _sign_post(
        priv, actor_url=actor_url, host="example.test", path="/inbox", body=body
    )
    tampered = b'{"type":"Delete"}'

    async with session_factory() as s:
        async with httpx.AsyncClient() as client:
            verified = await verify_signed_request(
                method="POST", path="/inbox", headers=headers,
                body=tampered, session=s, http_client=client,
            )
    assert verified is None


@pytest.mark.asyncio
async def test_verify_returns_none_when_key_cannot_be_resolved(
    session_factory: async_sessionmaker[AsyncSession],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, _pub = keypair
    actor_url = "https://nowhere.test/users/ghost"
    body = b'{"type":"Create"}'
    headers = _sign_post(
        priv, actor_url=actor_url, host="nowhere.test", path="/inbox", body=body
    )

    async with respx.mock() as router:
        router.get(actor_url).respond(status_code=404)
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                verified = await verify_signed_request(
                    method="POST", path="/inbox", headers=headers,
                    body=body, session=s, http_client=client,
                )
    assert verified is None


@pytest.mark.asyncio
async def test_verify_returns_none_when_signature_header_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        async with httpx.AsyncClient() as client:
            verified = await verify_signed_request(
                method="POST", path="/inbox",
                headers={"Host": "x", "Content-Type": "application/activity+json"},
                body=b"{}", session=s, http_client=client,
            )
    assert verified is None


@pytest.mark.asyncio
async def test_verify_returns_none_when_signature_malformed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        async with httpx.AsyncClient() as client:
            verified = await verify_signed_request(
                method="POST", path="/inbox",
                headers={
                    "Host": "x",
                    "Signature": "this-is-not-a-real-signature-header",
                },
                body=b"{}", session=s, http_client=client,
            )
    assert verified is None


@pytest.mark.asyncio
async def test_verify_strips_fragment_in_returned_actor_url(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """The returned actor URL must not carry the `#main-key` fragment —
    callers persist it as the canonical actor URI."""
    priv, pub = keypair
    actor_url = "https://example.test/users/alice"
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=102, username="alice", domain="example.test",
                uri=actor_url, public_key=pub.decode("utf-8"),
            )
        )
        s.add(seed_data["make_account_stat"](account_id=102))
        await s.commit()

    body = b'{"type":"Like"}'
    headers = _sign_post(
        priv, actor_url=actor_url, host="example.test", path="/inbox", body=body
    )
    async with session_factory() as s:
        async with httpx.AsyncClient() as client:
            verified = await verify_signed_request(
                method="POST", path="/inbox", headers=headers,
                body=body, session=s, http_client=client,
            )
    assert verified == actor_url  # no `#main-key` suffix
