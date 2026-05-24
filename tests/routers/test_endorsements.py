"""Tests for /accounts/{id}/pin /unpin + /api/v1/endorsements."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import AccountPin

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
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_pin_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/accounts/2/pin")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_pin_self_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/accounts/1/pin", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_pin_without_follow_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/accounts/2/pin", headers=_AUTH)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_pin_after_follow_succeeds(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    response = await client.post("/api/v1/accounts/2/pin", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["endorsed"] is True

    async with session_factory() as s:
        rows = (await s.execute(select(AccountPin))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_pin_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    await client.post("/api/v1/accounts/2/pin", headers=_AUTH)
    await client.post("/api/v1/accounts/2/pin", headers=_AUTH)

    async with session_factory() as s:
        rows = (await s.execute(select(AccountPin))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_unpin_removes_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    await client.post("/api/v1/accounts/2/pin", headers=_AUTH)

    response = await client.post("/api/v1/accounts/2/unpin", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["endorsed"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(AccountPin))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_endorsements_listing(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    await client.post("/api/v1/accounts/2/pin", headers=_AUTH)

    response = await client.get("/api/v1/endorsements", headers=_AUTH)
    body = response.json()
    assert [a["username"] for a in body] == ["bob"]


@pytest.mark.asyncio
async def test_relationship_endorsed_flag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    before = await client.get(
        "/api/v1/accounts/relationships?id[]=2", headers=_AUTH
    )
    assert before.json()[0]["endorsed"] is False

    await client.post("/api/v1/accounts/2/pin", headers=_AUTH)
    after = await client.get(
        "/api/v1/accounts/relationships?id[]=2", headers=_AUTH
    )
    assert after.json()[0]["endorsed"] is True
