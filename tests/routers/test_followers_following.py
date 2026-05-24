"""Tests for /api/v1/accounts/{id}/{followers,following}."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Account, Follow

_AUTH = {"Authorization": "Bearer raw-token-abc"}


def _make_follow(follow_id: int, *, follower: int, target: int) -> Follow:
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    return Follow(
        id=follow_id,
        account_id=follower,
        target_account_id=target,
        show_reblogs=True,
        notify=False,
        languages=None,
        uri=None,
        created_at=now,
        updated_at=now,
    )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """alice (1), bob (2 — target), carol (3), dave (4); plus token for alice."""
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account"](id_=3, username="carol"),
                seed_data["make_account"](id_=4, username="dave"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_account_stat"](account_id=3),
                seed_data["make_account_stat"](account_id=4),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_followers_lists_followers_desc_by_follow_id(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        # carol follows bob first, then dave (later id = more recent)
        s.add(_make_follow(10, follower=3, target=2))
        s.add(_make_follow(20, follower=4, target=2))
        await s.commit()

    response = await client.get("/api/v1/accounts/2/followers")
    assert response.status_code == 200
    usernames = [row["username"] for row in response.json()]
    assert usernames == ["dave", "carol"]  # most-recent follow first


@pytest.mark.asyncio
async def test_following_lists_targets(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        # bob follows carol then dave
        s.add(_make_follow(30, follower=2, target=3))
        s.add(_make_follow(40, follower=2, target=4))
        await s.commit()

    response = await client.get("/api/v1/accounts/2/following")
    usernames = [row["username"] for row in response.json()]
    assert usernames == ["dave", "carol"]


@pytest.mark.asyncio
async def test_pagination_uses_follow_id_as_cursor(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(_make_follow(10, follower=3, target=2))
        s.add(_make_follow(20, follower=4, target=2))
        await s.commit()

    page1 = await client.get("/api/v1/accounts/2/followers?limit=1")
    assert page1.json()[0]["username"] == "dave"
    assert "max_id=20" in page1.headers.get("Link", "")

    page2 = await client.get("/api/v1/accounts/2/followers?limit=1&max_id=20")
    assert page2.json()[0]["username"] == "carol"


@pytest.mark.asyncio
async def test_hide_collections_returns_empty_to_strangers(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        bob = (await s.execute(select(Account).where(Account.id == 2))).scalar_one()
        bob.hide_collections = True
        s.add(_make_follow(50, follower=3, target=2))
        await s.commit()

    anon = await client.get("/api/v1/accounts/2/followers")
    assert anon.json() == []

    # But bob viewing his own followers list still sees them.
    # (We don't have a token for bob in this fixture, so this case is
    # asserted by the policy unit-side; skipped here.)


@pytest.mark.asyncio
async def test_unknown_account_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/accounts/9999/followers")
    assert response.status_code == 404
