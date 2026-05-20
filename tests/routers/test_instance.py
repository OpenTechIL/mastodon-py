"""Tests for /api/v1/instance and /api/v2/instance."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_instance_v1_shape(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    # Seed a user + a few statuses so stats aren't all zero.
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob", domain="remote.social"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_status"](id_=100, account_id=1),
                seed_data["make_status"](id_=101, account_id=2, local=False),
            ]
        )
        await s.commit()

    response = await client.get("/api/v1/instance")
    assert response.status_code == 200
    body = response.json()
    # Static-ish fields
    assert "uri" in body
    assert "title" in body
    assert body["version"].startswith("4.")
    assert isinstance(body["urls"], dict)
    assert "streaming_api" in body["urls"]
    # Stats are live
    assert body["stats"]["user_count"] == 1
    assert body["stats"]["status_count"] == 1  # only local non-deleted
    assert body["stats"]["domain_count"] == 1


@pytest.mark.asyncio
async def test_instance_v2_shape(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v2/instance")
    assert response.status_code == 200
    body = response.json()
    assert body["domain"]
    assert "configuration" in body
    config = body["configuration"]
    assert config["statuses"]["max_characters"] == 500
    assert config["statuses"]["max_media_attachments"] == 4
    assert config["accounts"]["max_pinned_statuses"] == 5
    assert "vapid" in config
    assert "streaming" in config["urls"]


@pytest.mark.asyncio
async def test_instance_endpoints_do_not_require_auth(client: AsyncClient) -> None:
    """Anonymous clients (Mastodon-API-compatible apps before sign-in) must
    be able to read these to discover server capabilities."""
    v1 = await client.get("/api/v1/instance")
    v2 = await client.get("/api/v2/instance")
    assert v1.status_code == 200
    assert v2.status_code == 200
