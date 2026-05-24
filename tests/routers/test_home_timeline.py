"""Tests for GET /api/v1/timelines/home."""

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
    """alice (id=1, viewer), bob (id=2, followed), eve (id=3, not followed)."""
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account"](id_=3, username="eve"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_account_stat"](account_id=3),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_home_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/timelines/home")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_home_returns_followees_and_own(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # Alice follows bob.
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=2, text="from bob"))
        s.add(seed_data["make_status"](id_=200, account_id=3, text="from eve"))
        s.add(seed_data["make_status"](id_=300, account_id=1, text="from alice"))
        await s.commit()

    response = await client.get("/api/v1/timelines/home", headers=_AUTH)
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert ids == ["300", "100"]  # alice's own + bob's, descending; eve excluded


@pytest.mark.asyncio
async def test_home_excludes_deleted(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    naive_now,
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=400, account_id=1, deleted_at=naive_now))
        s.add(seed_data["make_status"](id_=500, account_id=1))
        await s.commit()

    response = await client.get("/api/v1/timelines/home", headers=_AUTH)
    ids = [row["id"] for row in response.json()]
    assert ids == ["500"]


@pytest.mark.asyncio
async def test_home_pagination_link_header(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        for i in range(5):
            s.add(seed_data["make_status"](id_=600 + i, account_id=1))
        await s.commit()

    response = await client.get("/api/v1/timelines/home?limit=2", headers=_AUTH)
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert ids == ["604", "603"]
    assert "max_id=603" in response.headers.get("Link", "")


@pytest.mark.asyncio
async def test_round_trip_post_then_home(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """End-to-end: alice posts -> appears on alice's home immediately."""
    await _seed(session_factory, seed_data)
    created = await client.post(
        "/api/v1/statuses",
        json={"status": "my first toot"},
        headers=_AUTH,
    )
    new_id = created.json()["id"]

    response = await client.get("/api/v1/timelines/home", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert any(row["id"] == new_id for row in body)
