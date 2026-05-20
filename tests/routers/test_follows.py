"""Tests for follow / unfollow / GET /api/v1/accounts/relationships."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import AccountStat, Follow, FollowRequest, Visibility


_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    bob_locked: bool = False,
    carol: bool = False,
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
                seed_data["make_account_stat"](account_id=2),
            ]
        )
        # Bob: separate so we can flip `locked` per test.
        bob_kwargs = {"id_": 2, "username": "bob"}
        bob = seed_data["make_account"](**bob_kwargs)
        bob.locked = bob_locked
        s.add(bob)
        if carol:
            s.add(seed_data["make_account"](id_=3, username="carol"))
            s.add(seed_data["make_account_stat"](account_id=3))
        await s.commit()


@pytest.mark.asyncio
async def test_follow_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/accounts/2/follow")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_follow_creates_row_and_bumps_counters(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    response = await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "2"
    assert body["following"] is True
    assert body["requested"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(Follow))).scalars().all()
        assert len(rows) == 1
        alice_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        bob_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 2))
        ).scalar_one()
        assert alice_stat.following_count == 1
        assert bob_stat.followers_count == 1


@pytest.mark.asyncio
async def test_follow_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    async with session_factory() as s:
        rows = (await s.execute(select(Follow))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_follow_self_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/accounts/1/follow", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_follow_unknown_target_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/accounts/999/follow", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_follow_locked_target_creates_request(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data, bob_locked=True)

    response = await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["following"] is False
    assert body["requested"] is True

    async with session_factory() as s:
        follows = (await s.execute(select(Follow))).scalars().all()
        requests = (await s.execute(select(FollowRequest))).scalars().all()
        assert follows == []
        assert len(requests) == 1
        # Pending request must not move counters yet.
        alice_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        assert alice_stat.following_count == 0


@pytest.mark.asyncio
async def test_unfollow_drops_follow_and_counters(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    response = await client.post("/api/v1/accounts/2/unfollow", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["following"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(Follow))).scalars().all()
        assert rows == []
        alice_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        assert alice_stat.following_count == 0


@pytest.mark.asyncio
async def test_unfollow_cancels_pending_request(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data, bob_locked=True)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    response = await client.post("/api/v1/accounts/2/unfollow", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["requested"] is False

    async with session_factory() as s:
        requests = (await s.execute(select(FollowRequest))).scalars().all()
        assert requests == []


@pytest.mark.asyncio
async def test_relationships_batched(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data, carol=True)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    # carol follows alice — exercises followed_by

    async with session_factory() as s:
        from datetime import datetime, timezone

        ts = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        s.add(
            Follow(
                id=99,
                account_id=3,
                target_account_id=1,
                show_reblogs=True,
                notify=False,
                languages=None,
                uri=None,
                created_at=ts,
                updated_at=ts,
            )
        )
        await s.commit()

    response = await client.get(
        "/api/v1/accounts/relationships?id[]=2&id[]=3",
        headers=_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    by_id = {r["id"]: r for r in body}
    assert by_id["2"]["following"] is True
    assert by_id["2"]["followed_by"] is False
    assert by_id["3"]["following"] is False
    assert by_id["3"]["followed_by"] is True


@pytest.mark.asyncio
async def test_private_status_visible_to_follower_not_random_authed_user(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice (viewer) tries to read bob's private status. Without a follow → 404."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=500, account_id=2, visibility=Visibility.PRIVATE))
        await s.commit()

    blocked = await client.get("/api/v1/statuses/500", headers=_AUTH)
    assert blocked.status_code == 404

    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)

    allowed = await client.get("/api/v1/statuses/500", headers=_AUTH)
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_direct_status_no_longer_visible_to_random_authed(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A direct message authored by bob isn't visible to alice even after a follow.

    (Mentions table isn't modeled — only the author qualifies.)
    """
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=600, account_id=2, visibility=Visibility.DIRECT))
        await s.commit()

    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    response = await client.get("/api/v1/statuses/600", headers=_AUTH)
    assert response.status_code == 404
