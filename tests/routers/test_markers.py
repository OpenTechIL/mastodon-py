"""Tests for /api/v1/markers (in `startup.py`).

Targets the existing handlers. Confirms:

  * GET requires auth + returns only timelines that exist.
  * POST upserts, bumps `lock_version` on second write (Rails
    optimistic-locking semantics: starts at 0, increments on update).
  * Both endpoints whitelist `home`/`notifications` — any other
    timeline key is silently dropped.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Marker


_AUTH = {"Authorization": "Bearer raw-token-abc"}


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
async def test_index_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/markers")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_index_returns_empty_when_no_markers_set(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get(
        "/api/v1/markers?timeline[]=home&timeline[]=notifications",
        headers=_AUTH,
    )
    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.asyncio
async def test_post_creates_markers_at_version_zero(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Rails optimistic-locking: first save persists with lock_version=0,
    only updates bump the column."""
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/markers",
        json={
            "home": {"last_read_id": "12345"},
            "notifications": {"last_read_id": "67890"},
        },
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["home"]["last_read_id"] == "12345"
    assert body["home"]["version"] == 0
    assert body["notifications"]["last_read_id"] == "67890"
    assert body["notifications"]["version"] == 0
    assert "updated_at" in body["home"]

    async with session_factory() as s:
        rows = (await s.execute(select(Marker))).scalars().all()
        assert {r.timeline for r in rows} == {"home", "notifications"}


@pytest.mark.asyncio
async def test_post_upsert_bumps_version_on_second_write(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    first = await client.post(
        "/api/v1/markers",
        json={"home": {"last_read_id": "100"}},
        headers=_AUTH,
    )
    assert first.json()["home"]["version"] == 0

    second = await client.post(
        "/api/v1/markers",
        json={"home": {"last_read_id": "200"}},
        headers=_AUTH,
    )
    body = second.json()
    assert body["home"]["last_read_id"] == "200"
    assert body["home"]["version"] == 1

    async with session_factory() as s:
        rows = (await s.execute(select(Marker))).scalars().all()
        assert len(rows) == 1
        assert rows[0].last_read_id == 200
        assert rows[0].lock_version == 1


@pytest.mark.asyncio
async def test_index_returns_only_requested_timelines(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/markers",
        json={
            "home": {"last_read_id": "1"},
            "notifications": {"last_read_id": "2"},
        },
        headers=_AUTH,
    )

    response = await client.get(
        "/api/v1/markers?timeline[]=home", headers=_AUTH
    )
    body = response.json()
    assert "home" in body
    assert "notifications" not in body


@pytest.mark.asyncio
async def test_post_ignores_unknown_timeline_keys(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Whitelist: anything outside home/notifications is silently dropped."""
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/markers",
        json={
            "home": {"last_read_id": "1"},
            "garbage": {"last_read_id": "2"},
            "public": {"last_read_id": "3"},
        },
        headers=_AUTH,
    )
    assert response.status_code == 200
    assert set(response.json().keys()) == {"home"}

    async with session_factory() as s:
        rows = (await s.execute(select(Marker))).scalars().all()
        assert {r.timeline for r in rows} == {"home"}


@pytest.mark.asyncio
async def test_post_with_non_integer_last_read_id_is_skipped(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/markers",
        json={"home": {"last_read_id": "not-a-number"}},
        headers=_AUTH,
    )
    assert response.status_code == 200
    assert response.json() == {}

    async with session_factory() as s:
        rows = (await s.execute(select(Marker))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_index_drops_unknown_timeline_query(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """`timeline[]=invalid` is dropped before the query — it doesn't
    leak into a stray SQL filter."""
    await _seed(session_factory, seed_data)
    response = await client.get(
        "/api/v1/markers?timeline[]=invalid", headers=_AUTH
    )
    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.asyncio
async def test_post_with_empty_body_is_noop(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/markers", json={}, headers=_AUTH
    )
    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.asyncio
async def test_post_with_null_payload_is_skipped(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Clients sometimes send `{home: null}` when nothing changed."""
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/markers",
        json={"home": None, "notifications": {"last_read_id": "5"}},
        headers=_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert "home" not in body
    assert body["notifications"]["last_read_id"] == "5"
