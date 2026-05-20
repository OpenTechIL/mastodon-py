"""Tests for `GET /api/v1/timelines/public`."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Visibility


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob", domain="remote.social"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_returns_only_public_non_reblog_non_reply_statuses(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        # Eligible
        s.add(seed_data["make_status"](id_=100, account_id=1))
        # Reblog -> excluded
        s.add(seed_data["make_status"](id_=101, account_id=1, reblog_of_id=100))
        # Reply to someone else -> excluded
        s.add(
            seed_data["make_status"](
                id_=102, account_id=1, reply=True, in_reply_to_account_id=2
            )
        )
        # Self-reply -> included
        s.add(
            seed_data["make_status"](
                id_=103, account_id=1, reply=True, in_reply_to_account_id=1
            )
        )
        # Private -> excluded
        s.add(seed_data["make_status"](id_=104, account_id=1, visibility=Visibility.PRIVATE))
        await s.commit()

    response = await client.get("/api/v1/timelines/public")
    assert response.status_code == 200
    ids = [s["id"] for s in response.json()]
    assert ids == ["103", "100"]  # descending


@pytest.mark.asyncio
async def test_local_filter_excludes_remote(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=1, local=True))
        s.add(
            seed_data["make_status"](
                id_=101, account_id=2, local=False, uri="https://remote.social/x"
            )
        )
        await s.commit()

    response = await client.get("/api/v1/timelines/public?local=true")
    ids = [s["id"] for s in response.json()]
    assert ids == ["100"]


@pytest.mark.asyncio
async def test_remote_filter_excludes_local(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=1, local=True))
        s.add(seed_data["make_status"](id_=101, account_id=2, local=False))
        await s.commit()

    response = await client.get("/api/v1/timelines/public?remote=true")
    ids = [s["id"] for s in response.json()]
    assert ids == ["101"]


@pytest.mark.asyncio
async def test_pagination_max_id_and_link_header(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        for i in range(5):
            s.add(seed_data["make_status"](id_=100 + i, account_id=1))
        await s.commit()

    page1 = await client.get("/api/v1/timelines/public?limit=2")
    assert page1.status_code == 200
    ids = [s["id"] for s in page1.json()]
    assert ids == ["104", "103"]
    link = page1.headers.get("Link", "")
    assert 'rel="next"' in link
    assert "max_id=103" in link

    page2 = await client.get("/api/v1/timelines/public?limit=2&max_id=103")
    ids2 = [s["id"] for s in page2.json()]
    assert ids2 == ["102", "101"]


@pytest.mark.asyncio
async def test_min_id_returns_newer_in_descending_order(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        for i in range(5):
            s.add(seed_data["make_status"](id_=100 + i, account_id=1))
        await s.commit()

    # min_id=101 should fetch ids strictly > 101, return closest to min_id first,
    # then re-order desc for the response body.
    response = await client.get("/api/v1/timelines/public?limit=2&min_id=101")
    ids = [s["id"] for s in response.json()]
    assert ids == ["103", "102"]


@pytest.mark.asyncio
async def test_empty_timeline_has_no_link_header(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/timelines/public")
    assert response.status_code == 200
    assert response.json() == []
    assert "Link" not in response.headers
