"""Tests for block / unblock / mute / unmute and their integration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Block, Follow, Mute

_AUTH = {"Authorization": "Bearer raw-token-abc"}


def _make_follow(follow_id: int, *, follower: int, target: int) -> Follow:
    ts = datetime.now(tz=UTC).replace(tzinfo=None)
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
    """alice (1, with token) and bob (2)."""
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
async def test_block_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/accounts/2/block")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_block_creates_row_and_relationship_flags(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/accounts/2/block", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["blocking"] is True
    assert body["blocked_by"] is False
    assert body["following"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(Block))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_block_tears_down_follows_both_ways(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(_make_follow(10, follower=1, target=2))
        s.add(_make_follow(11, follower=2, target=1))
        await s.commit()

    await client.post("/api/v1/accounts/2/block", headers=_AUTH)

    async with session_factory() as s:
        follows = (await s.execute(select(Follow))).scalars().all()
        assert follows == []


@pytest.mark.asyncio
async def test_block_refuses_follow(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/block", headers=_AUTH)

    response = await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_blocked_user_cannot_see_blockers_statuses(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Bob blocks alice. Alice (the viewer here) can no longer see bob's
    public statuses by id."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        # Bob posts publicly first
        s.add(seed_data["make_status"](id_=100, account_id=2, text="public"))
        s.add(seed_data["make_status_stat"](status_id=100))
        # Bob blocks alice (manually inserted to avoid needing bob's token)
        ts = datetime.now(tz=UTC).replace(tzinfo=None)
        s.add(
            Block(
                id=500,
                account_id=2,
                target_account_id=1,
                created_at=ts,
                updated_at=ts,
            )
        )
        await s.commit()

    response = await client.get("/api/v1/statuses/100", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unblock_removes_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/block", headers=_AUTH)
    response = await client.post("/api/v1/accounts/2/unblock", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["blocking"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(Block))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_self_block_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/accounts/1/block", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_mute_creates_row_and_relationship_flags(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/accounts/2/mute?notifications=false", headers=_AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["muting"] is True
    assert body["muting_notifications"] is False

    async with session_factory() as s:
        row = (await s.execute(select(Mute))).scalar_one()
        assert row.hide_notifications is False
        assert row.expires_at is None


@pytest.mark.asyncio
async def test_mute_with_duration_sets_expires_at(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/accounts/2/mute?duration=3600", headers=_AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["muting"] is True
    assert body["muting_expires_at"] is not None


@pytest.mark.asyncio
async def test_mute_excludes_target_from_home_timeline(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, account_id=2))
        await s.commit()

    before = await client.get("/api/v1/timelines/home", headers=_AUTH)
    assert {row["id"] for row in before.json()} == {"200"}

    await client.post("/api/v1/accounts/2/mute", headers=_AUTH)
    after = await client.get("/api/v1/timelines/home", headers=_AUTH)
    assert after.json() == []

    # Profile still shows their posts — mute hides home feed, not profiles.
    profile = await client.get("/api/v1/accounts/2/statuses", headers=_AUTH)
    assert {row["id"] for row in profile.json()} == {"200"}


@pytest.mark.asyncio
async def test_unmute_removes_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/mute", headers=_AUTH)

    response = await client.post("/api/v1/accounts/2/unmute", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["muting"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(Mute))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_repeat_mute_updates_settings(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/mute?notifications=true", headers=_AUTH)
    second = await client.post(
        "/api/v1/accounts/2/mute?notifications=false", headers=_AUTH
    )
    assert second.json()["muting_notifications"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(Mute))).scalars().all()
        assert len(rows) == 1
        assert rows[0].hide_notifications is False


@pytest.mark.asyncio
async def test_home_excludes_blocked_accounts(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=300, account_id=2))
        await s.commit()

    # Block tears down the follow; bob's posts should disappear from home.
    await client.post("/api/v1/accounts/2/block", headers=_AUTH)

    response = await client.get("/api/v1/timelines/home", headers=_AUTH)
    assert response.json() == []
