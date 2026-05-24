"""Tests for the federation fan-out hook in `post_status`.

These confirm that after a local user posts, the `deliver_activity`
job gets enqueued with the right arguments — but only when there are
actually remote followers to deliver to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.common.snowflake import now_id
from app.python.models import Account, Follow
from tests.conftest import FakeEnqueuer

_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed_local_alice(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> int:
    """Local alice + the OAuth scaffolding for her bearer token."""
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=1, username="alice", domain=None))
        s.add(seed_data["make_account_stat"](account_id=1))
        s.add(seed_data["make_user"](id_=1, account_id=1))
        s.add(seed_data["make_application"]())
        s.add(seed_data["make_token"]())
        await s.commit()
    return 1


async def _seed_remote_follower(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    id_: int,
    username: str,
    domain: str,
    inbox_url: str,
    shared_inbox_url: str = "",
    follow_target_id: int,
) -> None:
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=id_,
                username=username,
                domain=domain,
                uri=f"https://{domain}/users/{username}",
                inbox_url=inbox_url,
                shared_inbox_url=shared_inbox_url,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=id_))
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        s.add(
            Follow(
                id=now_id(),
                account_id=id_,
                target_account_id=follow_target_id,
                show_reblogs=True,
                notify=False,
                languages=None,
                uri=None,
                created_at=now,
                updated_at=now,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_public_post_with_no_remote_followers_does_not_enqueue(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """No remote followers → no fan-out job (local-only audience).
    Worth pinning so a future bug doesn't accidentally spam the queue
    with empty-recipient jobs."""
    await _seed_local_alice(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses", json={"status": "hello"}, headers=_AUTH
    )
    assert response.status_code == 200
    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert deliveries == []


@pytest.mark.asyncio
async def test_public_post_enqueues_deliver_activity_for_remote_followers(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    alice_id = await _seed_local_alice(session_factory, seed_data)
    await _seed_remote_follower(
        session_factory, seed_data,
        id_=100, username="bob", domain="other.test",
        inbox_url="https://other.test/users/bob/inbox",
        follow_target_id=alice_id,
    )
    response = await client.post(
        "/api/v1/statuses",
        json={"status": "hello federation"},
        headers=_AUTH,
    )
    assert response.status_code == 200

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert len(deliveries) == 1
    _name, args = deliveries[0]
    activity, sender_id, inbox_urls = args
    assert activity["type"] == "Create"
    assert activity["object"]["content"] == "hello federation"
    assert sender_id == alice_id
    assert inbox_urls == ["https://other.test/users/bob/inbox"]


@pytest.mark.asyncio
async def test_fanout_dedupes_followers_on_shared_inbox(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """Two followers on the same Mastodon-flavored server share one
    inbox URL — the enqueued list collapses to one entry."""
    alice_id = await _seed_local_alice(session_factory, seed_data)
    for i in range(2):
        await _seed_remote_follower(
            session_factory, seed_data,
            id_=100 + i, username=f"bob{i}", domain="other.test",
            inbox_url=f"https://other.test/users/bob{i}/inbox",
            shared_inbox_url="https://other.test/inbox",
            follow_target_id=alice_id,
        )
    response = await client.post(
        "/api/v1/statuses", json={"status": "hi"}, headers=_AUTH
    )
    assert response.status_code == 200

    [(_name, args)] = [
        c for c in fake_enqueuer.calls if c[0] == "deliver_activity"
    ]
    _activity, _sender_id, inbox_urls = args
    assert inbox_urls == ["https://other.test/inbox"]


@pytest.mark.asyncio
async def test_fanout_backfills_local_actor_keys_when_missing(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """First outbound post by an unkeyed local actor generates + persists
    an RSA keypair before enqueueing — otherwise the worker would
    silently drop the delivery (no key to sign with)."""
    alice_id = await _seed_local_alice(session_factory, seed_data)
    await _seed_remote_follower(
        session_factory, seed_data,
        id_=100, username="bob", domain="other.test",
        inbox_url="https://other.test/users/bob/inbox",
        follow_target_id=alice_id,
    )

    # Sanity: alice starts keyless (the seed factory defaults are blank).
    async with session_factory() as s:
        row = (
            await s.execute(select(Account).where(Account.id == alice_id))
        ).scalar_one()
        assert row.private_key == ""
        assert row.public_key == ""

    response = await client.post(
        "/api/v1/statuses", json={"status": "hi"}, headers=_AUTH
    )
    assert response.status_code == 200

    # Keys exist post-request — committed before the job was enqueued.
    async with session_factory() as s:
        row = (
            await s.execute(select(Account).where(Account.id == alice_id))
        ).scalar_one()
        assert row.private_key.startswith("-----BEGIN PRIVATE KEY-----")
        assert row.public_key.startswith("-----BEGIN PUBLIC KEY-----")

    # Delivery was enqueued — the worker reading this row will find a
    # populated key.
    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert len(deliveries) == 1


@pytest.mark.asyncio
async def test_direct_visibility_skips_fanout(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """DIRECT routes via mentions, not follower fan-out. Until mentions
    port, DIRECT posts have no federation side effect at all."""
    alice_id = await _seed_local_alice(session_factory, seed_data)
    await _seed_remote_follower(
        session_factory, seed_data,
        id_=100, username="bob", domain="other.test",
        inbox_url="https://other.test/users/bob/inbox",
        follow_target_id=alice_id,
    )
    response = await client.post(
        "/api/v1/statuses",
        json={"status": "@bob secret", "visibility": "direct"},
        headers=_AUTH,
    )
    assert response.status_code == 200

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert deliveries == []
