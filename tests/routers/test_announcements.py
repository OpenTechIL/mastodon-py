"""Tests for /api/v1/announcements + dismiss."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Announcement, AnnouncementMute


_AUTH = {"Authorization": "Bearer raw-token-abc"}


def _make_announcement(
    *,
    id_: int,
    text: str = "hello",
    published: bool = True,
    ends_at: datetime | None = None,
) -> Announcement:
    ts = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    return Announcement(
        id=id_,
        text=text,
        published=published,
        published_at=ts if published else None,
        starts_at=None,
        ends_at=ends_at,
        all_day=False,
        status_ids=None,
        created_at=ts,
        updated_at=ts,
    )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_index_returns_only_published(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(_make_announcement(id_=1, text="published"))
        s.add(_make_announcement(id_=2, text="draft", published=False))
        await s.commit()

    response = await client.get("/api/v1/announcements")
    body = response.json()
    assert [a["id"] for a in body] == ["1"]
    assert body[0]["content"] == "<p>published</p>"


@pytest.mark.asyncio
async def test_index_excludes_expired(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    yesterday = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    async with session_factory() as s:
        s.add(_make_announcement(id_=1, text="alive"))
        s.add(_make_announcement(id_=2, text="expired", ends_at=yesterday))
        await s.commit()

    response = await client.get("/api/v1/announcements")
    assert [a["id"] for a in response.json()] == ["1"]


@pytest.mark.asyncio
async def test_anonymous_sees_announcements_with_read_false(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(_make_announcement(id_=1))
        await s.commit()

    response = await client.get("/api/v1/announcements")
    assert response.status_code == 200
    assert response.json()[0]["read"] is False


@pytest.mark.asyncio
async def test_dismiss_flips_read_flag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(_make_announcement(id_=1))
        await s.commit()

    before = await client.get("/api/v1/announcements", headers=_AUTH)
    assert before.json()[0]["read"] is False

    response = await client.post("/api/v1/announcements/1/dismiss", headers=_AUTH)
    assert response.status_code == 200

    after = await client.get("/api/v1/announcements", headers=_AUTH)
    assert after.json()[0]["read"] is True

    async with session_factory() as s:
        rows = (await s.execute(select(AnnouncementMute))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_dismiss_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(_make_announcement(id_=1))
        await s.commit()

    await client.post("/api/v1/announcements/1/dismiss", headers=_AUTH)
    second = await client.post("/api/v1/announcements/1/dismiss", headers=_AUTH)
    assert second.status_code == 200

    async with session_factory() as s:
        rows = (await s.execute(select(AnnouncementMute))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_dismiss_unknown_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/announcements/9999/dismiss", headers=_AUTH
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/announcements/1/dismiss")
    assert response.status_code == 401
