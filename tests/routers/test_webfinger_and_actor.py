"""Tests for `.well-known/webfinger` and the AP actor JSON endpoint.

These are the discovery + identity endpoints remote Fediverse peers
hit before they can deliver to us. They're read-only, public.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEST_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0\n"
    "-----END PUBLIC KEY-----\n"
)


async def _seed_local_alice(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    display_name: str = "Alice",
    locked: bool = False,
) -> None:
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=1,
                username="alice",
                domain=None,
                display_name=display_name,
                locked=locked,
                public_key=_TEST_PUBLIC_KEY,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=1))
        await s.commit()


@pytest.mark.asyncio
async def test_webfinger_returns_jrd_for_local_user(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_local_alice(session_factory, seed_data)
    response = await client.get(
        "/.well-known/webfinger?resource=acct:alice@localhost:3000"
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/jrd+json")
    body = response.json()
    assert body["subject"] == "acct:alice@localhost:3000"
    # Self link points at the AP actor JSON.
    self_link = next(
        link for link in body["links"] if link["rel"] == "self"
    )
    assert self_link["type"] == "application/activity+json"
    assert self_link["href"].endswith("/users/alice")
    # Profile-page link points at HTML profile.
    profile_link = next(
        link for link in body["links"]
        if link["rel"] == "http://webfinger.net/rel/profile-page"
    )
    assert profile_link["type"] == "text/html"
    assert profile_link["href"].endswith("/@alice")


@pytest.mark.asyncio
async def test_webfinger_accepts_bare_user_at_host(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Some clients drop the `acct:` prefix — Mastodon accepts both."""
    await _seed_local_alice(session_factory, seed_data)
    response = await client.get(
        "/.well-known/webfinger?resource=alice@localhost:3000"
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_webfinger_404_for_unknown_user(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_local_alice(session_factory, seed_data)
    response = await client.get(
        "/.well-known/webfinger?resource=acct:nobody@localhost:3000"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_webfinger_404_for_wrong_host(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """We're authoritative only for our own domain — queries for
    `acct:alice@some.other.tld` get a 404 even if we have a local alice."""
    await _seed_local_alice(session_factory, seed_data)
    response = await client.get(
        "/.well-known/webfinger?resource=acct:alice@other.test"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_webfinger_400_for_malformed_resource(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/.well-known/webfinger?resource=not-a-valid-acct-uri"
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_webfinger_400_for_missing_resource(
    client: AsyncClient,
) -> None:
    response = await client.get("/.well-known/webfinger")
    # FastAPI's required-query-param validation → 422; either 400 or
    # 422 is acceptable here.
    assert response.status_code in (400, 422)


@pytest.mark.asyncio
async def test_actor_json_returns_person_object(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_local_alice(
        session_factory, seed_data, display_name="Alice the Great"
    )
    response = await client.get("/users/alice")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/activity+json"
    )
    body = response.json()
    assert body["type"] == "Person"
    assert body["preferredUsername"] == "alice"
    assert body["name"] == "Alice the Great"
    assert body["id"].endswith("/users/alice")
    assert body["inbox"].endswith("/users/alice/inbox")
    assert body["outbox"].endswith("/users/alice/outbox")
    assert body["followers"].endswith("/users/alice/followers")
    assert body["following"].endswith("/users/alice/following")
    # JSON-LD contexts: AS2 + security (for the publicKey block).
    assert "https://www.w3.org/ns/activitystreams" in body["@context"]
    assert "https://w3id.org/security/v1" in body["@context"]


@pytest.mark.asyncio
async def test_actor_json_includes_public_key(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Remote peers fetch the actor JSON to learn the public key they
    need to verify our outbound signatures."""
    await _seed_local_alice(session_factory, seed_data)
    response = await client.get("/users/alice")
    body = response.json()
    assert body["publicKey"]["id"].endswith("/users/alice#main-key")
    assert body["publicKey"]["owner"].endswith("/users/alice")
    assert body["publicKey"]["publicKeyPem"] == _TEST_PUBLIC_KEY


@pytest.mark.asyncio
async def test_actor_json_exposes_locked_as_manuallyApprovesFollowers(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """The legacy column name is `locked`; AP calls it
    `manuallyApprovesFollowers`. The mapping is fixed."""
    await _seed_local_alice(session_factory, seed_data, locked=True)
    response = await client.get("/users/alice")
    assert response.json()["manuallyApprovesFollowers"] is True


@pytest.mark.asyncio
async def test_actor_json_404_for_unknown_user(client: AsyncClient) -> None:
    response = await client.get("/users/nobody")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_actor_json_404_for_remote_lookalike(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A remote alice@other.test in our DB must not be served as our
    local actor — `/users/alice` is reserved for `domain IS NULL`."""
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=2, username="alice", domain="other.test",
                uri="https://other.test/users/alice",
            )
        )
        s.add(seed_data["make_account_stat"](account_id=2))
        await s.commit()
    response = await client.get("/users/alice")
    assert response.status_code == 404
