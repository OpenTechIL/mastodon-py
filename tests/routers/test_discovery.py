"""Tests for account search/lookup + trends stubs + instance peers/rules."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account"](id_=3, username="alicia"),  # substring match for "ali"
                seed_data["make_account"](id_=4, username="bob", domain="remote.social"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_account_stat"](account_id=3),
                seed_data["make_account_stat"](account_id=4),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


# ---------- account search ----------


@pytest.mark.asyncio
async def test_account_search_substring(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/accounts/search?q=ali")
    assert response.status_code == 200
    usernames = sorted(a["username"] for a in response.json())
    assert usernames == ["alice", "alicia"]


@pytest.mark.asyncio
async def test_account_search_empty_q_returns_empty(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/accounts/search?q=")
    assert response.json() == []


@pytest.mark.asyncio
async def test_account_search_following_filter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    everyone = await client.get(
        "/api/v1/accounts/search?q=bob", headers=_AUTH
    )
    just_followed = await client.get(
        "/api/v1/accounts/search?q=bob&following=true", headers=_AUTH
    )
    assert len(everyone.json()) == 2  # local bob + remote bob
    # Only bob#2 (the one we follow) survives the filter.
    just_following_acct = [a["acct"] for a in just_followed.json()]
    assert just_following_acct == ["bob"]


# ---------- account lookup ----------


@pytest.mark.asyncio
async def test_lookup_local(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/accounts/lookup?acct=alice")
    assert response.status_code == 200
    assert response.json()["id"] == "1"


@pytest.mark.asyncio
async def test_lookup_remote_by_acct(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get(
        "/api/v1/accounts/lookup?acct=bob@remote.social"
    )
    assert response.status_code == 200
    assert response.json()["id"] == "4"


@pytest.mark.asyncio
async def test_lookup_accepts_leading_at(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/accounts/lookup?acct=@alice")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_lookup_unknown_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/accounts/lookup?acct=ghost")
    assert response.status_code == 404


# ---------- trends stubs ----------


@pytest.mark.asyncio
async def test_trends_endpoints_return_empty(client: AsyncClient) -> None:
    for path in (
        "/api/v1/trends",
        "/api/v1/trends/tags",
        "/api/v1/trends/statuses",
        "/api/v1/trends/links",
    ):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json() == []


# ---------- instance peers + rules ----------


@pytest.mark.asyncio
async def test_instance_peers(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/instance/peers")
    assert response.status_code == 200
    # `remote.social` is bob#4's domain; nobody else has one.
    assert response.json() == ["remote.social"]


@pytest.mark.asyncio
async def test_instance_rules_returns_empty(client: AsyncClient) -> None:
    response = await client.get("/api/v1/instance/rules")
    assert response.status_code == 200
    assert response.json() == []
