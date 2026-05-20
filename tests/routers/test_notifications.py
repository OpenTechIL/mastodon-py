"""Tests for notification fan-out and /api/v1/notifications/*."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Notification


_AUTH = {"Authorization": "Bearer raw-token-abc"}
_BOB_TOKEN = "bob-token"
_BOB_AUTH = {"Authorization": f"Bearer {_BOB_TOKEN}"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """alice (1, local user, holds alice-token).
    bob (2, local user, holds bob-token).
    eve (3, NO user row — remote/cached account).
    """
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account"](id_=3, username="eve", domain="remote.social"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_account_stat"](account_id=3),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_user"](id_=2, account_id=2, email="bob@example.com"),
                seed_data["make_application"](),
                seed_data["make_token"](id_=1, token="raw-token-abc", resource_owner_id=1),
                seed_data["make_token"](id_=2, token=_BOB_TOKEN, resource_owner_id=2),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_favourite_notifies_status_author(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # Bob posts; alice favourites — bob (local) should get a notification.
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=2, text="hi"))
        s.add(seed_data["make_status_stat"](status_id=100))
        await s.commit()

    await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)

    response = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["type"] == "favourite"
    assert body[0]["account"]["username"] == "alice"
    assert body[0]["status"] is not None
    assert body[0]["status"]["id"] == "100"


@pytest.mark.asyncio
async def test_reblog_notifies_original_author(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, account_id=2, text="boost me"))
        s.add(seed_data["make_status_stat"](status_id=200))
        await s.commit()

    reblog = await client.post("/api/v1/statuses/200/reblog", headers=_AUTH)
    wrapper_id = reblog.json()["id"]

    response = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    body = response.json()
    assert len(body) == 1
    assert body[0]["type"] == "reblog"
    assert body[0]["account"]["username"] == "alice"
    # status is the wrapper boost, not the original.
    assert body[0]["status"]["id"] == wrapper_id
    assert body[0]["status"]["reblog"]["id"] == "200"


@pytest.mark.asyncio
async def test_follow_notifies_target(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    response = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    body = response.json()
    assert len(body) == 1
    assert body[0]["type"] == "follow"
    assert body[0]["account"]["username"] == "alice"
    assert body[0]["status"] is None


@pytest.mark.asyncio
async def test_remote_recipient_does_not_notify(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Eve is remote (no users row). Favourite/follow should NOT create a row."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=300, account_id=3, text="from remote"))
        s.add(seed_data["make_status_stat"](status_id=300))
        await s.commit()

    await client.post("/api/v1/statuses/300/favourite", headers=_AUTH)
    await client.post("/api/v1/accounts/3/follow", headers=_AUTH)

    async with session_factory() as s:
        rows = (await s.execute(select(Notification))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_self_action_does_not_notify(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # Alice posts and favourites her own status.
    created = await client.post(
        "/api/v1/statuses", json={"status": "narcissist mode"}, headers=_AUTH
    )
    sid = created.json()["id"]
    await client.post(f"/api/v1/statuses/{sid}/favourite", headers=_AUTH)

    response = await client.get("/api/v1/notifications", headers=_AUTH)
    assert response.json() == []


@pytest.mark.asyncio
async def test_types_filter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=400, account_id=2))
        s.add(seed_data["make_status_stat"](status_id=400))
        await s.commit()

    await client.post("/api/v1/statuses/400/favourite", headers=_AUTH)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    only_follows = await client.get(
        "/api/v1/notifications?types[]=follow", headers=_BOB_AUTH
    )
    assert {row["type"] for row in only_follows.json()} == {"follow"}

    no_follows = await client.get(
        "/api/v1/notifications?exclude_types[]=follow", headers=_BOB_AUTH
    )
    assert {row["type"] for row in no_follows.json()} == {"favourite"}


@pytest.mark.asyncio
async def test_dismiss_removes_single_notification(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    listing = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    nid = listing.json()[0]["id"]

    response = await client.post(
        f"/api/v1/notifications/{nid}/dismiss", headers=_BOB_AUTH
    )
    assert response.status_code == 200

    after = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    assert after.json() == []


@pytest.mark.asyncio
async def test_clear_empties_inbox(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=500, account_id=2))
        s.add(seed_data["make_status_stat"](status_id=500))
        await s.commit()

    await client.post("/api/v1/statuses/500/favourite", headers=_AUTH)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    before = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    assert len(before.json()) == 2

    cleared = await client.post("/api/v1/notifications/clear", headers=_BOB_AUTH)
    assert cleared.status_code == 200

    after = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    assert after.json() == []


@pytest.mark.asyncio
async def test_locked_target_generates_follow_request_notification(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        from app.python.models import Account
        bob = (await s.execute(select(Account).where(Account.id == 2))).scalar_one()
        bob.locked = True
        await s.commit()

    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    response = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    body = response.json()
    assert len(body) == 1
    assert body[0]["type"] == "follow_request"
