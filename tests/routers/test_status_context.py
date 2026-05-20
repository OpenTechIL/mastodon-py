"""Tests for GET /api/v1/statuses/{id}/context."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Visibility


_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
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
async def test_context_404_for_unknown(client: AsyncClient) -> None:
    response = await client.get("/api/v1/statuses/9999/context")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_context_ancestors_returned_root_first(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A → B → C reply chain. Asking for C's context returns [A, B] in order."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=1, text="root"))
        s.add(seed_data["make_status_stat"](status_id=100))
        s.add(
            seed_data["make_status"](
                id_=200,
                account_id=1,
                text="reply 1",
                reply=True,
                in_reply_to_id=100,
                in_reply_to_account_id=1,
            )
        )
        s.add(seed_data["make_status_stat"](status_id=200))
        s.add(
            seed_data["make_status"](
                id_=300,
                account_id=1,
                text="reply 2",
                reply=True,
                in_reply_to_id=200,
                in_reply_to_account_id=1,
            )
        )
        s.add(seed_data["make_status_stat"](status_id=300))
        await s.commit()

    response = await client.get("/api/v1/statuses/300/context")
    body = response.json()
    assert [s["id"] for s in body["ancestors"]] == ["100", "200"]
    assert body["descendants"] == []


@pytest.mark.asyncio
async def test_context_descendants_breadth_first(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Parent 100 has two direct replies (200, 201) and one grandchild (300 under 200)."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=1))
        s.add(seed_data["make_status_stat"](status_id=100))
        s.add(
            seed_data["make_status"](
                id_=200, account_id=1, reply=True, in_reply_to_id=100
            )
        )
        s.add(seed_data["make_status_stat"](status_id=200))
        s.add(
            seed_data["make_status"](
                id_=201, account_id=1, reply=True, in_reply_to_id=100
            )
        )
        s.add(seed_data["make_status_stat"](status_id=201))
        s.add(
            seed_data["make_status"](
                id_=300, account_id=1, reply=True, in_reply_to_id=200
            )
        )
        s.add(seed_data["make_status_stat"](status_id=300))
        await s.commit()

    response = await client.get("/api/v1/statuses/100/context")
    body = response.json()
    # Direct children first (depth 1: 200, 201), then grandchild (depth 2: 300).
    assert [s["id"] for s in body["descendants"]] == ["200", "201", "300"]
    assert body["ancestors"] == []


@pytest.mark.asyncio
async def test_context_excludes_private_replies_from_anon(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A public root with two replies — one public, one private. Anon sees only the public one."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=1))
        s.add(seed_data["make_status_stat"](status_id=100))
        s.add(
            seed_data["make_status"](
                id_=200,
                account_id=2,
                visibility=Visibility.PUBLIC,
                reply=True,
                in_reply_to_id=100,
            )
        )
        s.add(seed_data["make_status_stat"](status_id=200))
        s.add(
            seed_data["make_status"](
                id_=201,
                account_id=2,
                visibility=Visibility.PRIVATE,
                reply=True,
                in_reply_to_id=100,
            )
        )
        s.add(seed_data["make_status_stat"](status_id=201))
        await s.commit()

    anon = await client.get("/api/v1/statuses/100/context")
    assert [s["id"] for s in anon.json()["descendants"]] == ["200"]


@pytest.mark.asyncio
async def test_context_for_deleted_status_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    naive_now,
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(
            seed_data["make_status"](
                id_=100, account_id=1, deleted_at=naive_now
            )
        )
        await s.commit()

    response = await client.get("/api/v1/statuses/100/context")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_context_for_invisible_status_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Private status authored by bob; alice doesn't follow → 404."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=400, account_id=2, visibility=Visibility.PRIVATE))
        s.add(seed_data["make_status_stat"](status_id=400))
        await s.commit()

    response = await client.get("/api/v1/statuses/400/context", headers=_AUTH)
    assert response.status_code == 404
