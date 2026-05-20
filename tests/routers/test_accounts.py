"""Tests for `/api/v1/accounts/*`."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _seed_alice(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    with_token: bool = True,
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](note="hello there"),
                seed_data["make_account_stat"](
                    statuses_count=3, followers_count=10, following_count=5
                ),
                seed_data["make_user"](),
                seed_data["make_application"](),
            ]
        )
        if with_token:
            s.add(seed_data["make_token"]())
        await s.commit()


@pytest.mark.asyncio
async def test_verify_credentials_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/accounts/verify_credentials")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_verify_credentials_returns_account(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_alice(session_factory, seed_data)
    response = await client.get(
        "/api/v1/accounts/verify_credentials",
        headers={"Authorization": "Bearer raw-token-abc"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "1"
    assert body["username"] == "alice"
    assert body["acct"] == "alice"  # local
    assert body["statuses_count"] == 3
    assert body["followers_count"] == 10
    assert body["following_count"] == 5
    assert "<p>hello there</p>" == body["note"]
    assert body["avatar"].endswith("/avatars/original/missing.png")


@pytest.mark.asyncio
async def test_show_returns_public_account(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_alice(session_factory, seed_data, with_token=False)
    response = await client.get("/api/v1/accounts/1")
    assert response.status_code == 200
    assert response.json()["username"] == "alice"


@pytest.mark.asyncio
async def test_show_404_for_unknown_account(client: AsyncClient) -> None:
    response = await client.get("/api/v1/accounts/9999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_show_404_for_suspended_account(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    naive_now,
) -> None:
    async with session_factory() as s:
        s.add(seed_data["make_account"](suspended_at=naive_now))
        await s.commit()

    response = await client.get("/api/v1/accounts/1")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_remote_account_uses_at_acct_form(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add(seed_data["make_account"](username="bob", domain="other.social"))
        await s.commit()

    response = await client.get("/api/v1/accounts/1")
    assert response.status_code == 200
    assert response.json()["acct"] == "bob@other.social"


@pytest.mark.asyncio
async def test_bot_flag_from_actor_type(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add(seed_data["make_account"](actor_type="Service"))
        await s.commit()

    response = await client.get("/api/v1/accounts/1")
    assert response.json()["bot"] is True
    assert response.json()["group"] is False
