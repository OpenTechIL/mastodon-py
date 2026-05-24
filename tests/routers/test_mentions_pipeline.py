"""End-to-end mention pipeline tests."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Mention, Notification

_AUTH = {"Authorization": "Bearer raw-token-abc"}
_BOB_TOKEN = "bob-token"
_BOB_AUTH = {"Authorization": f"Bearer {_BOB_TOKEN}"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """alice (1, holds the main token), bob (2, local user, holds bob-token),
    eve (3, remote @eve@elsewhere.social), no carol (so unknown-remote
    mention testing has nothing to resolve to)."""
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account"](id_=3, username="eve", domain="elsewhere.social"),
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
async def test_post_creates_mention_rows_for_local_and_remote(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={"status": "hi @bob and @eve@elsewhere.social"},
        headers=_AUTH,
    )
    assert response.status_code == 200
    sid = int(response.json()["id"])

    async with session_factory() as s:
        rows = (
            await s.execute(select(Mention).where(Mention.status_id == sid))
        ).scalars().all()
        assert sorted(r.account_id for r in rows) == [2, 3]


@pytest.mark.asyncio
async def test_unknown_remote_mention_silently_skipped(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Webfinger lookup is deferred, so @stranger@unknown.tld doesn't
    materialize a Mention row."""
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={"status": "hi @stranger@nowhere.test"},
        headers=_AUTH,
    )
    assert response.status_code == 200

    async with session_factory() as s:
        rows = (await s.execute(select(Mention))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_self_mention_does_not_notify(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses", json={"status": "hi @alice"}, headers=_AUTH
    )

    async with session_factory() as s:
        rows = (await s.execute(select(Mention))).scalars().all()
        assert rows == []
        notifications = (await s.execute(select(Notification))).scalars().all()
        assert notifications == []


@pytest.mark.asyncio
async def test_local_mention_fires_notification(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses", json={"status": "hi @bob"}, headers=_AUTH
    )

    response = await client.get("/api/v1/notifications", headers=_BOB_AUTH)
    body = response.json()
    assert len(body) == 1
    assert body[0]["type"] == "mention"
    assert body[0]["account"]["username"] == "alice"
    assert body[0]["status"] is not None


@pytest.mark.asyncio
async def test_direct_visible_to_mentioned_recipient(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice DMs bob. Bob should see it via /api/v1/statuses/{id}."""
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "secret hi @bob", "visibility": "direct"},
        headers=_AUTH,
    )
    sid = posted.json()["id"]

    bob_view = await client.get(f"/api/v1/statuses/{sid}", headers=_BOB_AUTH)
    assert bob_view.status_code == 200


@pytest.mark.asyncio
async def test_direct_invisible_to_bystander(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice DMs bob. Carol (a third user) doesn't see it. We don't have
    a third token in fixtures; verify via anonymous instead — equally
    excluded from a DM."""
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "secret @bob", "visibility": "direct"},
        headers=_AUTH,
    )
    sid = posted.json()["id"]

    anon = await client.get(f"/api/v1/statuses/{sid}")
    assert anon.status_code == 404


@pytest.mark.asyncio
async def test_status_response_populates_mentions_list(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "hi @bob and @eve@elsewhere.social"},
        headers=_AUTH,
    )
    sid = posted.json()["id"]

    response = await client.get(f"/api/v1/statuses/{sid}", headers=_AUTH)
    body = response.json()
    accts = sorted(m["acct"] for m in body["mentions"])
    assert accts == ["bob", "eve@elsewhere.social"]


@pytest.mark.asyncio
async def test_private_status_visible_to_mention_without_follow(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A private (followers-only) status with a mention should also be
    visible to the mentioned account, even if they don't follow."""
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "private hello @bob", "visibility": "private"},
        headers=_AUTH,
    )
    sid = posted.json()["id"]

    bob_view = await client.get(f"/api/v1/statuses/{sid}", headers=_BOB_AUTH)
    assert bob_view.status_code == 200
