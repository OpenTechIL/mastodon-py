"""Tests for outbound Follow / Undo Follow.

Local user follows a remote → we enqueue a `deliver_activity` job
carrying a Follow activity for the target's inbox. Unfollow → Undo
Follow with the original Follow's URI as the inner object.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeEnqueuer


_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed_local_alice(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> int:
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=1, username="alice", domain=None))
        s.add(seed_data["make_account_stat"](account_id=1))
        s.add(seed_data["make_user"](id_=1, account_id=1))
        s.add(seed_data["make_application"]())
        s.add(seed_data["make_token"]())
        await s.commit()
    return 1


async def _seed_remote_bob(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    inbox_url: str = "https://other.test/users/bob/inbox",
    locked: bool = False,
) -> int:
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=100,
                username="bob",
                domain="other.test",
                uri="https://other.test/users/bob",
                inbox_url=inbox_url,
                locked=locked,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=100))
        await s.commit()
    return 100


@pytest.mark.asyncio
async def test_following_a_remote_enqueues_follow_activity(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    await _seed_local_alice(session_factory, seed_data)
    bob_id = await _seed_remote_bob(session_factory, seed_data)

    response = await client.post(
        f"/api/v1/accounts/{bob_id}/follow", headers=_AUTH
    )
    assert response.status_code == 200

    [(name, args)] = [
        c for c in fake_enqueuer.calls if c[0] == "deliver_activity"
    ]
    activity, sender_id, inbox_urls = args
    assert activity["type"] == "Follow"
    assert activity["actor"].endswith("/users/alice")
    assert activity["object"] == "https://other.test/users/bob"
    assert "#follows/" in activity["id"]  # stable URI for later Undo
    assert sender_id == 1
    assert inbox_urls == ["https://other.test/users/bob/inbox"]


@pytest.mark.asyncio
async def test_following_a_locked_remote_still_emits_follow(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """Locked target → local FollowRequest, but we still send the
    Follow activity. The remote server decides whether to send Accept
    back; from our side the wire shape is identical."""
    await _seed_local_alice(session_factory, seed_data)
    bob_id = await _seed_remote_bob(session_factory, seed_data, locked=True)

    await client.post(f"/api/v1/accounts/{bob_id}/follow", headers=_AUTH)

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert len(deliveries) == 1
    assert deliveries[0][1][0]["type"] == "Follow"


@pytest.mark.asyncio
async def test_following_a_local_target_does_not_enqueue(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """Local→local follow: no federation traffic."""
    await _seed_local_alice(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=2, username="carol", domain=None))
        s.add(seed_data["make_account_stat"](account_id=2))
        await s.commit()

    response = await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    assert response.status_code == 200

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert deliveries == []


@pytest.mark.asyncio
async def test_unfollowing_remote_enqueues_undo_with_original_uri(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """Undo's inner `object.id` must match the Follow's URI so the
    remote server can correlate the two — that's the contract peers
    rely on to remove the right Follow row."""
    await _seed_local_alice(session_factory, seed_data)
    bob_id = await _seed_remote_bob(session_factory, seed_data)

    # Follow first, capture its URI from the enqueued payload.
    await client.post(f"/api/v1/accounts/{bob_id}/follow", headers=_AUTH)
    [(_n, follow_args)] = [
        c for c in fake_enqueuer.calls if c[0] == "deliver_activity"
    ]
    follow_uri = follow_args[0]["id"]

    # Then unfollow.
    response = await client.post(
        f"/api/v1/accounts/{bob_id}/unfollow", headers=_AUTH
    )
    assert response.status_code == 200

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert len(deliveries) == 2
    undo_args = deliveries[1][1]
    undo_activity = undo_args[0]
    assert undo_activity["type"] == "Undo"
    assert undo_activity["object"]["type"] == "Follow"
    assert undo_activity["object"]["id"] == follow_uri


@pytest.mark.asyncio
async def test_unfollowing_local_target_does_not_enqueue(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    await _seed_local_alice(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=2, username="carol", domain=None))
        s.add(seed_data["make_account_stat"](account_id=2))
        await s.commit()

    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    await client.post("/api/v1/accounts/2/unfollow", headers=_AUTH)

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert deliveries == []
