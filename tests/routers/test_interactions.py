"""Tests for favourite / unfavourite / bookmark / unbookmark."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Bookmark, Favourite, StatusStat, Visibility


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
                seed_data["make_status"](id_=100, account_id=2, text="hi"),
                seed_data["make_status_stat"](status_id=100),
            ]
        )
        await s.commit()


_AUTH = {"Authorization": "Bearer raw-token-abc"}


@pytest.mark.asyncio
async def test_favourite_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/statuses/100/favourite")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_favourite_creates_row_and_bumps_counter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    response = await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["favourited"] is True
    assert body["favourites_count"] == 1

    async with session_factory() as s:
        rows = (await s.execute(select(Favourite))).scalars().all()
        assert len(rows) == 1
        stat = (await s.execute(select(StatusStat).where(StatusStat.status_id == 100))).scalar_one()
        assert stat.favourites_count == 1


@pytest.mark.asyncio
async def test_favourite_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)
    second = await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)
    assert second.status_code == 200
    assert second.json()["favourites_count"] == 1

    async with session_factory() as s:
        rows = (await s.execute(select(Favourite))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_unfavourite_removes_row_and_decrements_counter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)

    response = await client.post("/api/v1/statuses/100/unfavourite", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["favourited"] is False
    assert body["favourites_count"] == 0

    async with session_factory() as s:
        rows = (await s.execute(select(Favourite))).scalars().all()
        assert rows == []
        stat = (await s.execute(select(StatusStat).where(StatusStat.status_id == 100))).scalar_one()
        assert stat.favourites_count == 0


@pytest.mark.asyncio
async def test_unfavourite_when_not_favourited_is_noop(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/statuses/100/unfavourite", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["favourited"] is False


@pytest.mark.asyncio
async def test_favourite_invisible_status_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        # A direct message authored by bob targeting bob -- alice can't see it.
        s.add(seed_data["make_status"](id_=200, account_id=2, visibility=Visibility.DIRECT))
        await s.commit()

    response = await client.post("/api/v1/statuses/200/favourite", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_bookmark_round_trip(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    bookmark_resp = await client.post("/api/v1/statuses/100/bookmark", headers=_AUTH)
    assert bookmark_resp.status_code == 200
    assert bookmark_resp.json()["bookmarked"] is True

    async with session_factory() as s:
        rows = (await s.execute(select(Bookmark))).scalars().all()
        assert len(rows) == 1

    unb = await client.post("/api/v1/statuses/100/unbookmark", headers=_AUTH)
    assert unb.status_code == 200
    assert unb.json()["bookmarked"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(Bookmark))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_bookmark_does_not_affect_status_stats(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/bookmark", headers=_AUTH)

    async with session_factory() as s:
        stat = (
            await s.execute(select(StatusStat).where(StatusStat.status_id == 100))
        ).scalar_one()
        assert stat.favourites_count == 0


@pytest.mark.asyncio
async def test_get_status_show_reflects_viewer_favourite(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)

    anon = await client.get("/api/v1/statuses/100")
    assert anon.status_code == 200
    assert anon.json()["favourited"] is False  # anonymous viewer sees no flag
    assert anon.json()["favourites_count"] == 1

    authed = await client.get("/api/v1/statuses/100", headers=_AUTH)
    assert authed.status_code == 200
    assert authed.json()["favourited"] is True


@pytest.mark.asyncio
async def test_public_timeline_reflects_viewer_relationships(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)
    await client.post("/api/v1/statuses/100/bookmark", headers=_AUTH)

    response = await client.get("/api/v1/timelines/public", headers=_AUTH)
    body = response.json()
    assert len(body) == 1
    assert body[0]["favourited"] is True
    assert body[0]["bookmarked"] is True
