"""Tests for `/api/v1/statuses/{id}`."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Visibility


async def _seed_minimal(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](),
                seed_data["make_account_stat"](),
                seed_data["make_user"](),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_show_public_status_anon(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_minimal(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, text="hi"))
        s.add(seed_data["make_status_stat"](status_id=100, favourites_count=2))
        await s.commit()

    response = await client.get("/api/v1/statuses/100")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "100"
    assert body["content"] == "<p>hi</p>"
    assert body["visibility"] == "public"
    assert body["favourites_count"] == 2
    assert body["account"]["id"] == "1"


@pytest.mark.asyncio
async def test_show_private_status_anonymous_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_minimal(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, visibility=Visibility.PRIVATE))
        await s.commit()

    response = await client.get("/api/v1/statuses/200")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_show_private_status_authed_is_200(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_minimal(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, visibility=Visibility.PRIVATE))
        await s.commit()

    response = await client.get(
        "/api/v1/statuses/200",
        headers={"Authorization": "Bearer raw-token-abc"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_limited_visibility_masks_as_private_in_response(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_minimal(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=300, visibility=Visibility.LIMITED))
        await s.commit()

    response = await client.get(
        "/api/v1/statuses/300",
        headers={"Authorization": "Bearer raw-token-abc"},
    )
    assert response.status_code == 200
    assert response.json()["visibility"] == "private"


@pytest.mark.asyncio
async def test_deleted_status_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    naive_now,
) -> None:
    await _seed_minimal(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=400, deleted_at=naive_now))
        await s.commit()

    response = await client.get("/api/v1/statuses/400")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reblog_status_nests_original(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_minimal(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=10, text="original"))
        s.add(seed_data["make_status"](id_=20, reblog_of_id=10, text=""))
        await s.commit()

    response = await client.get("/api/v1/statuses/20")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "20"
    assert body["reblog"] is not None
    assert body["reblog"]["id"] == "10"
    assert body["reblog"]["content"] == "<p>original</p>"
