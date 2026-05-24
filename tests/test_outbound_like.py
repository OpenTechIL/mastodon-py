"""Tests for outbound Like / Undo Like.

Local user favourites a remote status → we enqueue a `deliver_activity`
job carrying a Like for the remote author's inbox. Unfavouriting
emits Undo Like with the original Like's URI cited.
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


async def _seed_remote_bob_with_status(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    inbox_url: str = "https://other.test/inbox",
    status_uri: str = "https://other.test/users/bob/statuses/77",
    status_id: int = 777,
) -> tuple[int, int]:
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=100, username="bob", domain="other.test",
                uri="https://other.test/users/bob",
                inbox_url=inbox_url,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=100))
        s.add(
            seed_data["make_status"](
                id_=status_id, account_id=100, text="hi", local=False,
                uri=status_uri,
            )
        )
        s.add(seed_data["make_status_stat"](status_id=status_id))
        await s.commit()
    return 100, status_id


@pytest.mark.asyncio
async def test_favouriting_remote_status_enqueues_like(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    await _seed_local_alice(session_factory, seed_data)
    _bob_id, status_id = await _seed_remote_bob_with_status(
        session_factory, seed_data
    )
    response = await client.post(
        f"/api/v1/statuses/{status_id}/favourite", headers=_AUTH
    )
    assert response.status_code == 200

    [(_n, args)] = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    activity, sender_id, inbox_urls = args
    assert activity["type"] == "Like"
    assert activity["actor"].endswith("/users/alice")
    assert activity["object"] == "https://other.test/users/bob/statuses/77"
    assert "#likes/" in activity["id"]
    assert sender_id == 1
    assert inbox_urls == ["https://other.test/inbox"]


@pytest.mark.asyncio
async def test_favouriting_local_status_does_not_federate(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """Favouriting our own (or another local user's) post stays local."""
    await _seed_local_alice(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=2, username="carol", domain=None))
        s.add(seed_data["make_account_stat"](account_id=2))
        s.add(seed_data["make_status"](id_=10, account_id=2, text="hi"))
        s.add(seed_data["make_status_stat"](status_id=10))
        await s.commit()

    response = await client.post(
        "/api/v1/statuses/10/favourite", headers=_AUTH
    )
    assert response.status_code == 200

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert deliveries == []


@pytest.mark.asyncio
async def test_unfavouriting_remote_status_emits_undo_like(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """The Undo wraps a Like inner object whose `id` matches the
    original Like — peers correlate by URI to remove the right row."""
    await _seed_local_alice(session_factory, seed_data)
    _bob_id, status_id = await _seed_remote_bob_with_status(
        session_factory, seed_data
    )
    # Favourite then unfavourite.
    await client.post(f"/api/v1/statuses/{status_id}/favourite", headers=_AUTH)
    [(_n, like_args)] = [
        c for c in fake_enqueuer.calls if c[0] == "deliver_activity"
    ]
    like_uri = like_args[0]["id"]

    response = await client.post(
        f"/api/v1/statuses/{status_id}/unfavourite", headers=_AUTH
    )
    assert response.status_code == 200

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert len(deliveries) == 2
    undo = deliveries[1][1][0]
    assert undo["type"] == "Undo"
    assert undo["object"]["type"] == "Like"
    assert undo["object"]["id"] == like_uri
    assert undo["object"]["object"] == "https://other.test/users/bob/statuses/77"
