"""End-to-end tests for `/api/v1/apps/verify_credentials`."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_verify_credentials_anonymous_is_401(client: AsyncClient) -> None:
    response = await client.get("/api/v1/apps/verify_credentials")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_verify_credentials_unknown_token_is_401(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/apps/verify_credentials",
        headers={"Authorization": "Bearer mystery"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_verify_credentials_returns_application(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](),
                seed_data["make_user"](),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()

    response = await client.get(
        "/api/v1/apps/verify_credentials",
        headers={"Authorization": "Bearer raw-token-abc"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "1"
    assert body["name"] == "Test Client"
    assert body["website"] == "https://example.com"
    assert body["scopes"] == ["read", "write"]
    assert body["redirect_uri"] == "urn:ietf:wg:oauth:2.0:oob"
    assert body["redirect_uris"] == ["urn:ietf:wg:oauth:2.0:oob"]
    assert "vapid_key" in body
