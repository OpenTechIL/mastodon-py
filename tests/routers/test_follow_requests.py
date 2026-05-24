"""Tests for /api/v1/follow_requests + authorize/reject."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Account, AccountStat, Follow, FollowRequest

_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """alice is the locked target (holds the token); bob requests to follow."""
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
        # Lock alice and create a pending request from bob -> alice.
        alice = (await s.execute(select(Account).where(Account.id == 1))).scalar_one()
        alice.locked = True
        ts = datetime.now(tz=UTC).replace(tzinfo=None)
        s.add(
            FollowRequest(
                id=10,
                account_id=2,
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


@pytest.mark.asyncio
async def test_index_lists_pending_requests(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/follow_requests", headers=_AUTH)
    assert response.status_code == 200
    assert [row["username"] for row in response.json()] == ["bob"]


@pytest.mark.asyncio
async def test_authorize_promotes_to_follow_and_bumps_counters(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    response = await client.post(
        "/api/v1/follow_requests/2/authorize", headers=_AUTH
    )
    assert response.status_code == 200
    rel = response.json()
    assert rel["id"] == "2"
    assert rel["followed_by"] is True
    assert rel["requested_by"] is False

    async with session_factory() as s:
        requests = (await s.execute(select(FollowRequest))).scalars().all()
        assert requests == []
        follows = (await s.execute(select(Follow))).scalars().all()
        assert len(follows) == 1
        assert follows[0].account_id == 2
        assert follows[0].target_account_id == 1
        alice_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        bob_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 2))
        ).scalar_one()
        assert alice_stat.followers_count == 1
        assert bob_stat.following_count == 1


@pytest.mark.asyncio
async def test_reject_removes_request_without_creating_follow(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/follow_requests/2/reject", headers=_AUTH)
    assert response.status_code == 200

    async with session_factory() as s:
        requests = (await s.execute(select(FollowRequest))).scalars().all()
        assert requests == []
        follows = (await s.execute(select(Follow))).scalars().all()
        assert follows == []
        # Counters didn't move.
        alice_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        assert alice_stat.followers_count == 0


@pytest.mark.asyncio
async def test_authorize_without_pending_request_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """No pending request from carol → can't authorize."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=3, username="carol"))
        s.add(seed_data["make_account_stat"](account_id=3))
        await s.commit()

    response = await client.post(
        "/api/v1/follow_requests/3/authorize", headers=_AUTH
    )
    assert response.status_code == 404
