"""Tests for /api/v1/accounts/{id}/statuses."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Visibility


_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed_two_accounts(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """alice (viewer, account 1), bob (target, account 2). Tokens for alice."""
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
async def test_returns_only_public_for_anon(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_two_accounts(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=2, visibility=Visibility.PUBLIC))
        s.add(seed_data["make_status"](id_=101, account_id=2, visibility=Visibility.UNLISTED))
        s.add(seed_data["make_status"](id_=102, account_id=2, visibility=Visibility.PRIVATE))
        s.add(seed_data["make_status"](id_=103, account_id=2, visibility=Visibility.DIRECT))
        await s.commit()

    response = await client.get("/api/v1/accounts/2/statuses")
    assert response.status_code == 200
    ids = sorted(row["id"] for row in response.json())
    assert ids == ["100", "101"]


@pytest.mark.asyncio
async def test_follower_sees_private_but_not_direct(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_two_accounts(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=2, visibility=Visibility.PUBLIC))
        s.add(seed_data["make_status"](id_=102, account_id=2, visibility=Visibility.PRIVATE))
        s.add(seed_data["make_status"](id_=103, account_id=2, visibility=Visibility.DIRECT))
        await s.commit()

    # Without following, alice sees only public.
    anon_view = await client.get("/api/v1/accounts/2/statuses", headers=_AUTH)
    assert {row["id"] for row in anon_view.json()} == {"100"}

    # After following bob, alice also sees private — but never direct.
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    follower_view = await client.get("/api/v1/accounts/2/statuses", headers=_AUTH)
    assert {row["id"] for row in follower_view.json()} == {"100", "102"}


@pytest.mark.asyncio
async def test_author_sees_own_public_unlisted_private_but_not_direct(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_two_accounts(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, account_id=1, visibility=Visibility.PUBLIC))
        s.add(seed_data["make_status"](id_=201, account_id=1, visibility=Visibility.PRIVATE))
        s.add(seed_data["make_status"](id_=202, account_id=1, visibility=Visibility.DIRECT))
        await s.commit()

    response = await client.get("/api/v1/accounts/1/statuses", headers=_AUTH)
    assert {row["id"] for row in response.json()} == {"200", "201"}


@pytest.mark.asyncio
async def test_exclude_replies(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_two_accounts(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=2))
        # Reply to someone else's status — should be filtered out
        s.add(
            seed_data["make_status"](
                id_=101, account_id=2, reply=True, in_reply_to_account_id=1
            )
        )
        # Self-reply — kept
        s.add(
            seed_data["make_status"](
                id_=102, account_id=2, reply=True, in_reply_to_account_id=2
            )
        )
        await s.commit()

    response = await client.get("/api/v1/accounts/2/statuses?exclude_replies=true")
    ids = sorted(row["id"] for row in response.json())
    assert ids == ["100", "102"]


@pytest.mark.asyncio
async def test_exclude_reblogs(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_two_accounts(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=2))
        s.add(seed_data["make_status"](id_=101, account_id=2, reblog_of_id=100))
        await s.commit()

    response = await client.get("/api/v1/accounts/2/statuses?exclude_reblogs=true")
    ids = sorted(row["id"] for row in response.json())
    assert ids == ["100"]


@pytest.mark.asyncio
async def test_pinned_returns_empty_until_modeled(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_two_accounts(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=2))
        await s.commit()

    response = await client.get("/api/v1/accounts/2/statuses?pinned=true")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_suspended_target_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    naive_now,
) -> None:
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=5, username="ghost", suspended_at=naive_now))
        await s.commit()

    response = await client.get("/api/v1/accounts/5/statuses")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_pagination_link_header(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed_two_accounts(session_factory, seed_data)
    async with session_factory() as s:
        for i in range(4):
            s.add(seed_data["make_status"](id_=200 + i, account_id=2))
        await s.commit()

    response = await client.get("/api/v1/accounts/2/statuses?limit=2")
    ids = [row["id"] for row in response.json()]
    assert ids == ["203", "202"]
    assert "max_id=202" in response.headers.get("Link", "")
