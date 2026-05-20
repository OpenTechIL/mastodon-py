"""Tests for /favourites, /bookmarks, /blocks, /mutes listings."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
                seed_data["make_account"](id_=3, username="carol"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_account_stat"](account_id=3),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
                seed_data["make_status"](id_=100, account_id=2),
                seed_data["make_status_stat"](status_id=100),
                seed_data["make_status"](id_=101, account_id=3),
                seed_data["make_status_stat"](status_id=101),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_favourites_lists_caller_faves_only_in_desc(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)
    await client.post("/api/v1/statuses/101/favourite", headers=_AUTH)

    response = await client.get("/api/v1/favourites", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    # Most-recent fave first.
    assert [row["id"] for row in body] == ["101", "100"]


@pytest.mark.asyncio
async def test_favourites_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/favourites")).status_code == 401


@pytest.mark.asyncio
async def test_bookmarks_lists_round_trip(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/bookmark", headers=_AUTH)
    response = await client.get("/api/v1/bookmarks", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == ["100"]


@pytest.mark.asyncio
async def test_blocks_listing(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/block", headers=_AUTH)
    await client.post("/api/v1/accounts/3/block", headers=_AUTH)

    response = await client.get("/api/v1/blocks", headers=_AUTH)
    body = response.json()
    # Most-recent block first.
    assert [row["username"] for row in body] == ["carol", "bob"]


@pytest.mark.asyncio
async def test_mutes_listing(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/mute", headers=_AUTH)

    response = await client.get("/api/v1/mutes", headers=_AUTH)
    assert [row["username"] for row in response.json()] == ["bob"]


@pytest.mark.asyncio
async def test_pagination_link_header(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses/100/favourite", headers=_AUTH)
    await client.post("/api/v1/statuses/101/favourite", headers=_AUTH)

    response = await client.get("/api/v1/favourites?limit=1", headers=_AUTH)
    assert response.status_code == 200
    assert len(response.json()) == 1
    # Link header advertises a max_id (the Favourite.id, not the Status.id).
    link = response.headers.get("Link", "")
    assert "max_id=" in link
    # Confirm the cursor is NOT the status id "101" — that's the entity id.
    # (We can't easily check the join id; but we can check ordering by
    # asking for the next page and confirming we get the other status.)
    import re
    next_cursor = re.search(r"max_id=(\d+)", link).group(1)
    page2 = await client.get(
        f"/api/v1/favourites?limit=1&max_id={next_cursor}", headers=_AUTH
    )
    assert [row["id"] for row in page2.json()] == ["100"]
