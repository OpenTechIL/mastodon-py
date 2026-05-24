"""Tests for inbound Accept{Follow} and Reject{Follow} ActivityPub activities."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.federation.activity import clear_activity_dedup_cache, dispatch
from app.python.models import Follow, FollowRequest


@pytest.fixture(autouse=True)
def reset_activity_dedup() -> None:
    clear_activity_dedup_cache()


_LOCAL_ALICE = "https://mastodon.test/users/alice"
_REMOTE_BOB = "https://other.test/users/bob"

_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0\n"
    "-----END PUBLIC KEY-----\n"
)


def _actor_json(actor_url: str) -> dict[str, Any]:
    return {
        "id": actor_url,
        "type": "Person",
        "preferredUsername": actor_url.split("/")[-1],
        "name": "Bob",
        "inbox": f"{actor_url}/inbox",
        "publicKey": {"id": f"{actor_url}#key", "owner": actor_url, "publicKeyPem": _PEM},
        "manuallyApprovesFollowers": True,
    }


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        # Local alice
        s.add(seed_data["make_account"](id_=1, username="alice", domain=None, uri=_LOCAL_ALICE))
        s.add(seed_data["make_account_stat"](account_id=1))
        # Remote bob (locked)
        s.add(
            seed_data["make_account"](
                id_=100,
                username="bob",
                domain="other.test",
                uri=_REMOTE_BOB,
                inbox_url=f"{_REMOTE_BOB}/inbox",
                locked=True,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=100))
        await s.commit()


async def _seed_follow_request(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    follow_uri: str = "https://mastodon.test/users/alice#follows/100",
) -> None:
    async with session_factory() as s:
        from app.python.models import FollowRequest
        from app.python.common.snowflake import now_id
        from datetime import datetime, UTC
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        s.add(
            FollowRequest(
                id=now_id(),
                account_id=1,
                target_account_id=100,
                uri=follow_uri,
                show_reblogs=True,
                notify=False,
                languages=None,
                created_at=now,
                updated_at=now,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_accept_follow_promotes_request_to_follow(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    follow_uri = "https://mastodon.test/users/alice#follows/100"
    await _seed_follow_request(session_factory, seed_data, follow_uri)

    accept_activity: dict[str, Any] = {
        "type": "Accept",
        "id": f"{_REMOTE_BOB}#accepts/follows/1",
        "actor": _REMOTE_BOB,
        "object": {
            "type": "Follow",
            "id": follow_uri,
            "actor": _LOCAL_ALICE,
            "object": _REMOTE_BOB,
        },
    }

    async with respx.mock(assert_all_called=False):
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                await dispatch(
                    session=s,
                    http_client=client,
                    enqueuer=None,
                    actor_url=_REMOTE_BOB,
                    activity=accept_activity,
                )

    async with session_factory() as s:
        requests = (await s.execute(select(FollowRequest))).scalars().all()
        follows = (await s.execute(select(Follow))).scalars().all()

    assert requests == [], "FollowRequest should be removed after Accept"
    assert len(follows) == 1
    assert follows[0].account_id == 1
    assert follows[0].target_account_id == 100


@pytest.mark.asyncio
async def test_accept_follow_by_uri(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Accept where the inner object is just the Follow URI (not embedded)."""
    await _seed(session_factory, seed_data)
    follow_uri = "https://mastodon.test/users/alice#follows/100"
    await _seed_follow_request(session_factory, seed_data, follow_uri)

    accept_activity: dict[str, Any] = {
        "type": "Accept",
        "id": f"{_REMOTE_BOB}#accepts/follows/1",
        "actor": _REMOTE_BOB,
        "object": follow_uri,  # URI-only
    }

    async with respx.mock(assert_all_called=False):
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                await dispatch(
                    session=s,
                    http_client=client,
                    enqueuer=None,
                    actor_url=_REMOTE_BOB,
                    activity=accept_activity,
                )

    async with session_factory() as s:
        follows = (await s.execute(select(Follow))).scalars().all()
    assert len(follows) == 1


@pytest.mark.asyncio
async def test_reject_follow_removes_request(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    follow_uri = "https://mastodon.test/users/alice#follows/100"
    await _seed_follow_request(session_factory, seed_data, follow_uri)

    reject_activity: dict[str, Any] = {
        "type": "Reject",
        "id": f"{_REMOTE_BOB}#rejects/follows/1",
        "actor": _REMOTE_BOB,
        "object": {
            "type": "Follow",
            "id": follow_uri,
            "actor": _LOCAL_ALICE,
            "object": _REMOTE_BOB,
        },
    }

    async with respx.mock(assert_all_called=False):
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                await dispatch(
                    session=s,
                    http_client=client,
                    enqueuer=None,
                    actor_url=_REMOTE_BOB,
                    activity=reject_activity,
                )

    async with session_factory() as s:
        requests = (await s.execute(select(FollowRequest))).scalars().all()
        follows = (await s.execute(select(Follow))).scalars().all()

    assert requests == [], "FollowRequest should be removed after Reject"
    assert follows == [], "No Follow should exist after Reject"


@pytest.mark.asyncio
async def test_accept_follow_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Re-delivery of Accept should not error — just a no-op if no request exists."""
    await _seed(session_factory, seed_data)
    # No FollowRequest seeded — Accept with no pending request is a no-op.

    accept_activity: dict[str, Any] = {
        "type": "Accept",
        "id": f"{_REMOTE_BOB}#accepts/follows/999",
        "actor": _REMOTE_BOB,
        "object": {
            "type": "Follow",
            "id": "https://mastodon.test/users/alice#follows/999",
            "actor": _LOCAL_ALICE,
            "object": _REMOTE_BOB,
        },
    }

    async with respx.mock(assert_all_called=False):
        async with session_factory() as s:
            async with httpx.AsyncClient() as client:
                await dispatch(
                    session=s,
                    http_client=client,
                    enqueuer=None,
                    actor_url=_REMOTE_BOB,
                    activity=accept_activity,
                )

    async with session_factory() as s:
        follows = (await s.execute(select(Follow))).scalars().all()
    assert follows == []
