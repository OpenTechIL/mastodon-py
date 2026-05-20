"""Tests for the actor key resolver."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.federation.key_resolver import resolve_public_key


def _make_keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, bytes]:
    return _make_keypair()


async def _seed_remote_account(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    uri: str,
    public_key_pem: bytes,
) -> None:
    """Insert a `domain != NULL` Account with the given URI + key."""
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=100,
                username="alice",
                domain="example.test",
                uri=uri,
                public_key=public_key_pem.decode("utf-8"),
            )
        )
        s.add(seed_data["make_account_stat"](account_id=100))
        await s.commit()


@pytest.mark.asyncio
async def test_local_hit_skips_http(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    _priv, pub = keypair
    actor_url = "https://example.test/users/alice"
    key_id = f"{actor_url}#main-key"
    await _seed_remote_account(
        session_factory, seed_data, uri=actor_url, public_key_pem=pub
    )

    async with respx.mock(assert_all_called=False) as router:
        # No route registered — any HTTP call would 502. Local hit
        # means we never make one.
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                result = await resolve_public_key(
                    key_id=key_id, session=s, http_client=client
                )
        assert result == pub
        assert not any(call.has_response for call in router.calls)


@pytest.mark.asyncio
async def test_remote_fetch_returns_pem(
    session_factory: async_sessionmaker[AsyncSession],
    keypair: tuple[bytes, bytes],
) -> None:
    _priv, pub = keypair
    actor_url = "https://other.test/users/bob"
    key_id = f"{actor_url}#main-key"
    actor_json = {
        "id": actor_url,
        "type": "Person",
        "publicKey": {
            "id": key_id,
            "owner": actor_url,
            "publicKeyPem": pub.decode("utf-8"),
        },
    }

    async with respx.mock() as router:
        router.get(actor_url).respond(
            json=actor_json,
            headers={"content-type": "application/activity+json"},
        )
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                result = await resolve_public_key(
                    key_id=key_id, session=s, http_client=client
                )
        assert result == pub
        assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_remote_fetch_picks_matching_key_from_list(
    session_factory: async_sessionmaker[AsyncSession],
    keypair: tuple[bytes, bytes],
) -> None:
    """Actors may advertise multiple keys (rotation). The resolver
    picks the one whose `id` matches the keyId in the signature."""
    _priv, target_pub = keypair
    other_priv, other_pub = _make_keypair()
    actor_url = "https://other.test/users/bob"
    target_key_id = f"{actor_url}#main-key"
    actor_json = {
        "id": actor_url,
        "publicKey": [
            {
                "id": f"{actor_url}#old-key",
                "publicKeyPem": other_pub.decode("utf-8"),
            },
            {
                "id": target_key_id,
                "publicKeyPem": target_pub.decode("utf-8"),
            },
        ],
    }

    async with respx.mock() as router:
        router.get(actor_url).respond(json=actor_json)
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                result = await resolve_public_key(
                    key_id=target_key_id, session=s, http_client=client
                )
        assert result == target_pub
        # Sanity: the other key was an option but not picked.
        assert result != other_pub


@pytest.mark.asyncio
async def test_remote_fetch_returns_none_on_404(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor_url = "https://gone.test/users/missing"
    key_id = f"{actor_url}#main-key"
    async with respx.mock() as router:
        router.get(actor_url).respond(status_code=404)
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                result = await resolve_public_key(
                    key_id=key_id, session=s, http_client=client
                )
        assert result is None


@pytest.mark.asyncio
async def test_remote_fetch_returns_none_on_missing_public_key_field(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor_url = "https://other.test/users/keyless"
    key_id = f"{actor_url}#main-key"
    async with respx.mock() as router:
        router.get(actor_url).respond(
            json={"id": actor_url, "type": "Person"}
        )
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                result = await resolve_public_key(
                    key_id=key_id, session=s, http_client=client
                )
        assert result is None


@pytest.mark.asyncio
async def test_remote_fetch_returns_none_on_invalid_pem(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `publicKeyPem` field that isn't a recognizable PEM block must
    not be returned — the verifier would crash on it later."""
    actor_url = "https://other.test/users/garbage"
    key_id = f"{actor_url}#main-key"
    async with respx.mock() as router:
        router.get(actor_url).respond(
            json={
                "id": actor_url,
                "publicKey": {"id": key_id, "publicKeyPem": "not-a-pem"},
            }
        )
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                result = await resolve_public_key(
                    key_id=key_id, session=s, http_client=client
                )
        assert result is None


@pytest.mark.asyncio
async def test_non_http_scheme_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A keyId that isn't an http(s) URL never triggers a fetch."""
    async with session_factory() as s:
        async with httpx.AsyncClient() as client:
            result = await resolve_public_key(
                key_id="acct:alice@example.test", session=s, http_client=client
            )
    assert result is None


@pytest.mark.asyncio
async def test_local_hit_with_empty_public_key_falls_back_to_http(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    """If we have a row but no key on it yet, fetch — the row's
    presence isn't the contract, the populated key is."""
    _priv, pub = keypair
    actor_url = "https://example.test/users/alice"
    key_id = f"{actor_url}#main-key"
    await _seed_remote_account(
        session_factory, seed_data, uri=actor_url, public_key_pem=b""
    )

    actor_json = {
        "id": actor_url,
        "publicKey": {
            "id": key_id,
            "publicKeyPem": pub.decode("utf-8"),
        },
    }
    async with respx.mock() as router:
        router.get(actor_url).respond(json=actor_json)
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                result = await resolve_public_key(
                    key_id=key_id, session=s, http_client=client
                )
        assert result == pub
        assert router.calls.call_count == 1
