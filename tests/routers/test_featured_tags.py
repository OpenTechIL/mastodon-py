"""Tests for /api/v1/featured_tags + /accounts/{id}/featured_tags."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import FeaturedTag


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


async def _post_with_tag(client: AsyncClient, text: str) -> None:
    """Posting creates the Tag rows via the hashtag pipeline."""
    response = await client.post(
        "/api/v1/statuses", json={"status": text}, headers=_AUTH
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_index_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/featured_tags")).status_code == 401


@pytest.mark.asyncio
async def test_crud_round_trip(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await _post_with_tag(client, "post #python")

    empty = await client.get("/api/v1/featured_tags", headers=_AUTH)
    assert empty.json() == []

    created = await client.post(
        "/api/v1/featured_tags", json={"name": "python"}, headers=_AUTH
    )
    assert created.status_code == 200
    body = created.json()
    ft_id = body["id"]
    assert body["name"].lower() == "python"
    # Statuses count should reflect the one tagged post we made.
    assert body["statuses_count"] == "1"

    listed = await client.get("/api/v1/featured_tags", headers=_AUTH)
    assert len(listed.json()) == 1

    destroyed = await client.delete(
        f"/api/v1/featured_tags/{ft_id}", headers=_AUTH
    )
    assert destroyed.status_code == 200

    async with session_factory() as s:
        rows = (await s.execute(select(FeaturedTag))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_cannot_feature_unused_tag(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/featured_tags",
        json={"name": "neverused"},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_max_ten_featured(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # Post 11 statuses with distinct tags so we can attempt 11 features.
    for i in range(11):
        await _post_with_tag(client, f"#tag{i}")

    for i in range(10):
        response = await client.post(
            "/api/v1/featured_tags",
            json={"name": f"tag{i}"},
            headers=_AUTH,
        )
        assert response.status_code == 200

    # The eleventh should fail.
    eleventh = await client.post(
        "/api/v1/featured_tags",
        json={"name": "tag10"},
        headers=_AUTH,
    )
    assert eleventh.status_code == 422


@pytest.mark.asyncio
async def test_idempotent_feature(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await _post_with_tag(client, "#repeat")
    first = await client.post(
        "/api/v1/featured_tags", json={"name": "repeat"}, headers=_AUTH
    )
    second = await client.post(
        "/api/v1/featured_tags", json={"name": "repeat"}, headers=_AUTH
    )
    assert first.json()["id"] == second.json()["id"]
    async with session_factory() as s:
        rows = (await s.execute(select(FeaturedTag))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_accounts_featured_tags_is_public(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await _post_with_tag(client, "post #python")
    await client.post(
        "/api/v1/featured_tags", json={"name": "python"}, headers=_AUTH
    )

    # No auth header — anonymous viewers see alice's featured tags.
    response = await client.get("/api/v1/accounts/1/featured_tags")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"].lower() == "python"


@pytest.mark.asyncio
async def test_delete_other_accounts_featured_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Bob owns a featured tag; alice can't delete it."""
    await _seed(session_factory, seed_data)
    from datetime import datetime, timezone

    ts = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    async with session_factory() as s:
        from app.python.models import Tag

        s.add(
            Tag(
                id=42,
                name="bobs",
                display_name="bobs",
                created_at=ts,
                updated_at=ts,
            )
        )
        s.add(
            FeaturedTag(
                id=99,
                account_id=2,  # bob
                tag_id=42,
                name="bobs",
                statuses_count=0,
                last_status_at=None,
                created_at=ts,
                updated_at=ts,
            )
        )
        await s.commit()

    response = await client.delete("/api/v1/featured_tags/99", headers=_AUTH)
    assert response.status_code == 404
