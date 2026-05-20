"""Tests for /api/v1/lists* and /api/v1/timelines/list/{id}."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Follow, List, ListAccount


_AUTH = {"Authorization": "Bearer raw-token-abc"}


def _make_follow(follow_id: int, *, follower: int, target: int) -> Follow:
    ts = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    return Follow(
        id=follow_id,
        account_id=follower,
        target_account_id=target,
        show_reblogs=True,
        notify=False,
        languages=None,
        uri=None,
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
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account"](id_=3, username="carol"),
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
async def test_list_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/lists")).status_code == 401


@pytest.mark.asyncio
async def test_crud_round_trip(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    empty = await client.get("/api/v1/lists", headers=_AUTH)
    assert empty.json() == []

    created = await client.post(
        "/api/v1/lists",
        json={"title": "Close Friends"},
        headers=_AUTH,
    )
    assert created.status_code == 200
    body = created.json()
    list_id = body["id"]
    assert body["title"] == "Close Friends"
    assert body["replies_policy"] == "followed"
    assert body["exclusive"] is False

    listed = await client.get("/api/v1/lists", headers=_AUTH)
    assert len(listed.json()) == 1

    shown = await client.get(f"/api/v1/lists/{list_id}", headers=_AUTH)
    assert shown.json()["id"] == list_id

    updated = await client.put(
        f"/api/v1/lists/{list_id}",
        json={"title": "Friends", "replies_policy": "list", "exclusive": True},
        headers=_AUTH,
    )
    body = updated.json()
    assert body["title"] == "Friends"
    assert body["replies_policy"] == "list"
    assert body["exclusive"] is True

    destroyed = await client.delete(f"/api/v1/lists/{list_id}", headers=_AUTH)
    assert destroyed.status_code == 200

    async with session_factory() as s:
        rows = (await s.execute(select(List))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_show_other_owners_list_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Bob owns a list; alice (the token holder) can't see it."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        ts = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        s.add(
            List(
                id=99,
                account_id=2,  # bob
                title="bob's",
                replies_policy=0,
                exclusive=False,
                created_at=ts,
                updated_at=ts,
            )
        )
        await s.commit()

    response = await client.get("/api/v1/lists/99", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_add_member_requires_follow(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    created = await client.post(
        "/api/v1/lists", json={"title": "L"}, headers=_AUTH
    )
    list_id = created.json()["id"]

    # Not following bob yet -> 422
    refused = await client.post(
        f"/api/v1/lists/{list_id}/accounts",
        json={"account_ids": [2]},
        headers=_AUTH,
    )
    assert refused.status_code == 422

    # Add the follow, retry
    async with session_factory() as s:
        s.add(_make_follow(10, follower=1, target=2))
        await s.commit()

    allowed = await client.post(
        f"/api/v1/lists/{list_id}/accounts",
        json={"account_ids": [2]},
        headers=_AUTH,
    )
    assert allowed.status_code == 200

    async with session_factory() as s:
        rows = (await s.execute(select(ListAccount))).scalars().all()
        assert len(rows) == 1
        assert rows[0].follow_id == 10


@pytest.mark.asyncio
async def test_remove_member(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    created = await client.post(
        "/api/v1/lists", json={"title": "L"}, headers=_AUTH
    )
    list_id = created.json()["id"]
    async with session_factory() as s:
        s.add(_make_follow(10, follower=1, target=2))
        await s.commit()
    await client.post(
        f"/api/v1/lists/{list_id}/accounts",
        json={"account_ids": [2]},
        headers=_AUTH,
    )

    listed = await client.get(
        f"/api/v1/lists/{list_id}/accounts", headers=_AUTH
    )
    assert [a["username"] for a in listed.json()] == ["bob"]

    await client.request(
        "DELETE",
        f"/api/v1/lists/{list_id}/accounts",
        json={"account_ids": [2]},
        headers=_AUTH,
    )
    cleared = await client.get(
        f"/api/v1/lists/{list_id}/accounts", headers=_AUTH
    )
    assert cleared.json() == []


@pytest.mark.asyncio
async def test_list_timeline_returns_only_members(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    created = await client.post(
        "/api/v1/lists", json={"title": "L"}, headers=_AUTH
    )
    list_id = created.json()["id"]
    async with session_factory() as s:
        # alice follows bob, then add bob to the list. carol is also followed
        # but isn't in the list.
        s.add(_make_follow(10, follower=1, target=2))
        s.add(_make_follow(11, follower=1, target=3))
        await s.commit()
    await client.post(
        f"/api/v1/lists/{list_id}/accounts",
        json={"account_ids": [2]},
        headers=_AUTH,
    )

    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, account_id=2))
        s.add(seed_data["make_status"](id_=201, account_id=3))
        await s.commit()

    response = await client.get(
        f"/api/v1/timelines/list/{list_id}", headers=_AUTH
    )
    ids = [row["id"] for row in response.json()]
    assert ids == ["200"]
