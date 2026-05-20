"""Tests for /api/v1/statuses/{id}/pin and ?pinned=true filter."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import StatusPin, Visibility


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
                seed_data["make_status"](id_=100, account_id=1, text="alice's"),
                seed_data["make_status_stat"](status_id=100),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_pin_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/statuses/100/pin")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_pin_creates_row_and_sets_flag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/statuses/100/pin", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["pinned"] is True

    async with session_factory() as s:
        rows = (await s.execute(select(StatusPin))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_pin_non_author_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, account_id=2, text="bob's"))
        s.add(seed_data["make_status_stat"](status_id=200))
        await s.commit()

    response = await client.post("/api/v1/statuses/200/pin", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_pin_private_status_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=300, account_id=1, visibility=Visibility.PRIVATE))
        s.add(seed_data["make_status_stat"](status_id=300))
        await s.commit()

    response = await client.post("/api/v1/statuses/300/pin", headers=_AUTH)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_pin_max_five(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        for i in range(101, 106):  # 5 more statuses, ids 101..105
            s.add(seed_data["make_status"](id_=i, account_id=1))
            s.add(seed_data["make_status_stat"](status_id=i))
        await s.commit()

    for i in range(101, 106):
        response = await client.post(f"/api/v1/statuses/{i}/pin", headers=_AUTH)
        assert response.status_code == 200

    # Sixth pin (status 100) should fail with 422.
    sixth = await client.post("/api/v1/statuses/100/pin", headers=_AUTH)
    assert sixth.status_code == 422


@pytest.mark.asyncio
async def test_unpin_removes_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/pin", headers=_AUTH)

    response = await client.post("/api/v1/statuses/100/unpin", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["pinned"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(StatusPin))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_pinned_filter_returns_pinned_only(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=400, account_id=1, text="other"))
        s.add(seed_data["make_status_stat"](status_id=400))
        await s.commit()

    await client.post("/api/v1/statuses/100/pin", headers=_AUTH)

    response = await client.get("/api/v1/accounts/1/statuses?pinned=true")
    ids = [row["id"] for row in response.json()]
    assert ids == ["100"]
