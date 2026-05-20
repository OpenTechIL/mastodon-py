"""Tests for /api/v2/search."""

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
async def test_search_empty_q_returns_empty(client: AsyncClient) -> None:
    response = await client.get("/api/v2/search?q=")
    body = response.json()
    assert body == {"accounts": [], "statuses": [], "hashtags": []}


@pytest.mark.asyncio
async def test_search_returns_all_three_types(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Posting `hello #hello` creates the Tag, the Status, and accounts already exist."""
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses",
        json={"status": "hello world #hello"},
        headers=_AUTH,
    )

    response = await client.get("/api/v2/search?q=hello")
    assert response.status_code == 200
    body = response.json()
    # accounts: none match "hello"
    assert body["accounts"] == []
    # statuses: the post matches
    assert len(body["statuses"]) == 1
    # hashtags: the #hello tag matches
    assert [t["name"].lower() for t in body["hashtags"]] == ["hello"]


@pytest.mark.asyncio
async def test_search_type_filter_narrows(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses", json={"status": "post #alice"}, headers=_AUTH
    )

    accounts_only = await client.get("/api/v2/search?q=alice&type=accounts")
    assert len(accounts_only.json()["accounts"]) == 1
    assert accounts_only.json()["statuses"] == []
    assert accounts_only.json()["hashtags"] == []

    tags_only = await client.get("/api/v2/search?q=alice&type=hashtags")
    assert tags_only.json()["accounts"] == []
    assert tags_only.json()["statuses"] == []
    assert [t["name"].lower() for t in tags_only.json()["hashtags"]] == ["alice"]


@pytest.mark.asyncio
async def test_search_excludes_private_statuses_from_anon(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses",
        json={"status": "public secret", "visibility": "public"},
        headers=_AUTH,
    )
    await client.post(
        "/api/v1/statuses",
        json={"status": "private secret", "visibility": "private"},
        headers=_AUTH,
    )

    anon = await client.get("/api/v2/search?q=secret")
    contents = sorted(s["content"] for s in anon.json()["statuses"])
    assert contents == ["<p>public secret</p>"]

    # Author sees both (their own private).
    authed = await client.get("/api/v2/search?q=secret", headers=_AUTH)
    authed_contents = sorted(s["content"] for s in authed.json()["statuses"])
    assert authed_contents == ["<p>private secret</p>", "<p>public secret</p>"]


@pytest.mark.asyncio
async def test_search_anonymous_pagination_blocked(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v2/search?q=anything&offset=10")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_search_strips_hash_prefix(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Clients commonly send `?q=#tag` — the `#` should be stripped."""
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses", json={"status": "#example post"}, headers=_AUTH
    )
    response = await client.get("/api/v2/search?q=%23example&type=hashtags")
    assert [t["name"].lower() for t in response.json()["hashtags"]] == ["example"]


@pytest.mark.asyncio
async def test_search_suspended_accounts_excluded(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    naive_now,
) -> None:
    async with session_factory() as s:
        s.add(seed_data["make_account"](id_=10, username="ghost", suspended_at=naive_now))
        await s.commit()
    response = await client.get("/api/v2/search?q=ghost&type=accounts")
    assert response.json()["accounts"] == []
