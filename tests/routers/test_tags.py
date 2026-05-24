"""Tests for tag timelines + tag follow + post-time hashtag extraction."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import FeaturedTag, StatusTag, Tag, TagFollow, Visibility


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
async def test_posting_extracts_hashtags(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={"status": "Hello #python and #FastAPI #python"},
        headers=_AUTH,
    )
    assert response.status_code == 200
    new_status_id = int(response.json()["id"])

    async with session_factory() as s:
        tags = (await s.execute(select(Tag).order_by(Tag.name.asc()))).scalars().all()
        # De-duped and lowercased; two distinct tags
        assert sorted(t.name for t in tags) == ["fastapi", "python"]
        joins = (
            await s.execute(
                select(StatusTag).where(StatusTag.status_id == new_status_id)
            )
        ).scalars().all()
        assert len(joins) == 2


@pytest.mark.asyncio
async def test_tag_timeline_returns_only_tagged_public(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # bob posts publicly and privately about #python; carol's not tagged
    await client.post(
        "/api/v1/statuses",
        json={"status": "alice public #python"},
        headers=_AUTH,
    )
    await client.post(
        "/api/v1/statuses",
        json={"status": "alice private #python", "visibility": "private"},
        headers=_AUTH,
    )
    await client.post(
        "/api/v1/statuses",
        json={"status": "unrelated post no tag"},
        headers=_AUTH,
    )

    response = await client.get("/api/v1/timelines/tag/python")
    assert response.status_code == 200
    body = response.json()
    # Private status excluded from tag timeline; untagged status excluded.
    assert len(body) == 1
    assert "#python" in body[0]["content"] or "python" in body[0]["content"]


@pytest.mark.asyncio
async def test_tag_timeline_case_insensitive(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses",
        json={"status": "post about #Python"},
        headers=_AUTH,
    )

    for url in ("/api/v1/timelines/tag/python", "/api/v1/timelines/tag/PYTHON"):
        response = await client.get(url)
        assert response.status_code == 200
        assert len(response.json()) == 1


@pytest.mark.asyncio
async def test_tag_show_includes_following_flag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses",
        json={"status": "post #python"},
        headers=_AUTH,
    )

    show = await client.get("/api/v1/tags/python", headers=_AUTH)
    assert show.status_code == 200
    assert show.json()["following"] is False

    await client.post("/api/v1/tags/python/follow", headers=_AUTH)
    show2 = await client.get("/api/v1/tags/python", headers=_AUTH)
    assert show2.json()["following"] is True

    async with session_factory() as s:
        rows = (await s.execute(select(TagFollow))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_tag_unfollow(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses", json={"status": "#go"}, headers=_AUTH
    )
    await client.post("/api/v1/tags/go/follow", headers=_AUTH)

    response = await client.post("/api/v1/tags/go/unfollow", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["following"] is False

    async with session_factory() as s:
        assert (await s.execute(select(TagFollow))).scalars().all() == []


@pytest.mark.asyncio
async def test_tag_show_404_for_unknown(client: AsyncClient) -> None:
    response = await client.get("/api/v1/tags/never-posted")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_tag_timeline_returns_empty_for_unknown(client: AsyncClient) -> None:
    """Mastodon returns 200 with empty body, not 404, for unknown tags."""
    response = await client.get("/api/v1/timelines/tag/never-posted")
    assert response.status_code == 200
    assert response.json() == []


# ---------- tag feature / unfeature ----------


@pytest.mark.asyncio
async def test_tag_feature_creates_featured_tag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses", json={"status": "#rust"}, headers=_AUTH)

    response = await client.post("/api/v1/tags/rust/feature", headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["name"] == "rust"

    async with session_factory() as s:
        rows = (await s.execute(select(FeaturedTag))).scalars().all()
        assert len(rows) == 1
        assert rows[0].account_id == 1


@pytest.mark.asyncio
async def test_tag_feature_is_idempotent(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses", json={"status": "#elixir"}, headers=_AUTH)

    await client.post("/api/v1/tags/elixir/feature", headers=_AUTH)
    await client.post("/api/v1/tags/elixir/feature", headers=_AUTH)

    async with session_factory() as s:
        rows = (await s.execute(select(FeaturedTag))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_tag_unfeature_removes_featured_tag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/statuses", json={"status": "#erlang"}, headers=_AUTH)
    await client.post("/api/v1/tags/erlang/feature", headers=_AUTH)

    response = await client.post("/api/v1/tags/erlang/unfeature", headers=_AUTH)
    assert response.status_code == 200

    async with session_factory() as s:
        rows = (await s.execute(select(FeaturedTag))).scalars().all()
        assert rows == []
