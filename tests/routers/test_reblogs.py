"""Tests for reblog / unreblog."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import AccountStat, Status, StatusStat, Visibility

_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """alice (id=1) is the actor, bob (id=2) authors the parent status 100."""
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account_stat"](account_id=1, statuses_count=0),
                seed_data["make_account_stat"](account_id=2, statuses_count=1),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
                seed_data["make_status"](id_=100, account_id=2, text="hi"),
                seed_data["make_status_stat"](status_id=100),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_reblog_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/statuses/100/reblog")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_reblog_creates_wrapper_and_bumps_counters(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    response = await client.post("/api/v1/statuses/100/reblog", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()

    # Wrapper status id is not 100; nested reblog points back at the original.
    assert body["id"] != "100"
    assert body["reblog"] is not None
    assert body["reblog"]["id"] == "100"
    assert body["reblog"]["reblogged"] is True
    assert body["reblog"]["reblogs_count"] == 1

    async with session_factory() as s:
        boost = (
            await s.execute(select(Status).where(Status.reblog_of_id == 100))
        ).scalar_one()
        assert boost.account_id == 1
        parent_stat = (
            await s.execute(select(StatusStat).where(StatusStat.status_id == 100))
        ).scalar_one()
        assert parent_stat.reblogs_count == 1
        actor_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        assert actor_stat.statuses_count == 1


@pytest.mark.asyncio
async def test_reblog_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    first = await client.post("/api/v1/statuses/100/reblog", headers=_AUTH)
    second = await client.post("/api/v1/statuses/100/reblog", headers=_AUTH)

    assert first.json()["id"] == second.json()["id"]
    assert second.json()["reblog"]["reblogs_count"] == 1

    async with session_factory() as s:
        rows = (
            await s.execute(select(Status).where(Status.reblog_of_id == 100))
        ).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_reblogging_a_reblog_chains_to_root(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        # Bob first reblogs his own status (acceptable for the test). The
        # request below targets the boost; the service should reblog the
        # root (100), not the boost (101).
        s.add(seed_data["make_status"](id_=101, account_id=2, reblog_of_id=100))
        await s.commit()

    response = await client.post("/api/v1/statuses/101/reblog", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["reblog"]["id"] == "100"


@pytest.mark.asyncio
async def test_reblog_of_private_status_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, account_id=2, visibility=Visibility.PRIVATE))
        await s.commit()

    response = await client.post("/api/v1/statuses/200/reblog", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unreblog_discards_wrapper_decrements_counters_returns_original(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/reblog", headers=_AUTH)

    response = await client.post("/api/v1/statuses/100/unreblog", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "100"  # original, not the discarded wrapper
    assert body["reblogged"] is False
    assert body["reblogs_count"] == 0

    async with session_factory() as s:
        wrappers = (
            await s.execute(select(Status).where(Status.reblog_of_id == 100))
        ).scalars().all()
        assert len(wrappers) == 1
        assert wrappers[0].deleted_at is not None
        actor_stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        assert actor_stat.statuses_count == 0


@pytest.mark.asyncio
async def test_unreblog_when_not_reblogged_is_noop(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    response = await client.post("/api/v1/statuses/100/unreblog", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["reblogged"] is False
    assert response.json()["reblogs_count"] == 0


@pytest.mark.asyncio
async def test_get_status_reflects_viewer_reblog(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/reblog", headers=_AUTH)

    anon = await client.get("/api/v1/statuses/100")
    assert anon.json()["reblogged"] is False

    authed = await client.get("/api/v1/statuses/100", headers=_AUTH)
    assert authed.json()["reblogged"] is True


@pytest.mark.asyncio
async def test_undoing_reblog_flips_viewer_flag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/reblog", headers=_AUTH)
    await client.post("/api/v1/statuses/100/unreblog", headers=_AUTH)

    authed = await client.get("/api/v1/statuses/100", headers=_AUTH)
    assert authed.json()["reblogged"] is False
