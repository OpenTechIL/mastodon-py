"""Tests for outbound Announce / Undo Announce.

Audience: the booster's remote followers + the original author's
inbox (when remote), deduped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.common.snowflake import now_id
from app.python.models import Follow
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


async def _seed_remote_bob_with_post(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    status_id: int = 777,
    status_uri: str = "https://other.test/users/bob/statuses/77",
    inbox_url: str = "https://other.test/users/bob/inbox",
) -> int:
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
    return status_id


async def _seed_remote_follower(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    id_: int,
    username: str,
    domain: str,
    inbox_url: str,
    follows_account_id: int,
) -> None:
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=id_, username=username, domain=domain,
                uri=f"https://{domain}/users/{username}",
                inbox_url=inbox_url,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=id_))
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        s.add(
            Follow(
                id=now_id(),
                account_id=id_,
                target_account_id=follows_account_id,
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
async def test_reblogging_remote_status_enqueues_announce(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """Alice (us) reblogs Bob's (remote) post → Announce delivered at
    least to Bob's inbox."""
    await _seed_local_alice(session_factory, seed_data)
    status_id = await _seed_remote_bob_with_post(session_factory, seed_data)

    response = await client.post(
        f"/api/v1/statuses/{status_id}/reblog", headers=_AUTH
    )
    assert response.status_code == 200

    [(_n, args)] = [
        c for c in fake_enqueuer.calls if c[0] == "deliver_activity"
    ]
    activity, sender_id, inbox_urls = args
    assert activity["type"] == "Announce"
    assert activity["actor"].endswith("/users/alice")
    assert activity["object"] == "https://other.test/users/bob/statuses/77"
    assert sender_id == 1
    assert "https://other.test/users/bob/inbox" in inbox_urls


@pytest.mark.asyncio
async def test_reblog_audience_includes_local_actors_remote_followers(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """Alice's remote followers see the boost in their home timeline →
    they must be in the Announce audience too."""
    alice_id = await _seed_local_alice(session_factory, seed_data)
    status_id = await _seed_remote_bob_with_post(session_factory, seed_data)
    await _seed_remote_follower(
        session_factory, seed_data,
        id_=200, username="carol", domain="third.test",
        inbox_url="https://third.test/users/carol/inbox",
        follows_account_id=alice_id,
    )

    await client.post(f"/api/v1/statuses/{status_id}/reblog", headers=_AUTH)

    [(_n, args)] = [
        c for c in fake_enqueuer.calls if c[0] == "deliver_activity"
    ]
    _activity, _sender_id, inbox_urls = args
    assert set(inbox_urls) == {
        "https://other.test/users/bob/inbox",
        "https://third.test/users/carol/inbox",
    }


@pytest.mark.asyncio
async def test_reblog_of_local_status_does_not_federate(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    """Alice reblogs a local post with no remote followers → no
    Announce fan-out (parent author is local; no remote audience)."""
    await _seed_local_alice(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=2, username="carol", domain=None))
        s.add(seed_data["make_account_stat"](account_id=2))
        s.add(
            seed_data["make_status"](
                id_=50, account_id=2, text="local post",
                uri="http://test/users/carol/statuses/50",
            )
        )
        s.add(seed_data["make_status_stat"](status_id=50))
        await s.commit()

    response = await client.post("/api/v1/statuses/50/reblog", headers=_AUTH)
    assert response.status_code == 200

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert deliveries == []


@pytest.mark.asyncio
async def test_unreblog_remote_emits_undo_announce(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    fake_enqueuer: FakeEnqueuer,
) -> None:
    await _seed_local_alice(session_factory, seed_data)
    status_id = await _seed_remote_bob_with_post(session_factory, seed_data)

    await client.post(f"/api/v1/statuses/{status_id}/reblog", headers=_AUTH)
    [(_n, announce_args)] = [
        c for c in fake_enqueuer.calls if c[0] == "deliver_activity"
    ]
    announce_uri = announce_args[0]["id"]

    response = await client.post(
        f"/api/v1/statuses/{status_id}/unreblog", headers=_AUTH
    )
    assert response.status_code == 200

    deliveries = [c for c in fake_enqueuer.calls if c[0] == "deliver_activity"]
    assert len(deliveries) == 2
    undo = deliveries[1][1][0]
    assert undo["type"] == "Undo"
    assert undo["object"]["type"] == "Announce"
    assert undo["object"]["id"] == announce_uri
    assert undo["object"]["object"] == "https://other.test/users/bob/statuses/77"
