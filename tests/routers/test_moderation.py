"""Tests for /api/v1/reports + /domain_blocks + /suggestions."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import AccountDomainBlock, Report


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
                seed_data["make_account"](id_=3, username="eve", domain="spam.social"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_account_stat"](account_id=3),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


# ---------- reports ----------


@pytest.mark.asyncio
async def test_report_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/reports", json={"account_id": 2}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_report(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/reports",
        json={
            "account_id": 2,
            "status_ids": [100, 101],
            "comment": "spam everywhere",
            "category": "spam",
            "rule_ids": [3],
            "forward": True,
        },
        headers=_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "spam"
    assert body["comment"] == "spam everywhere"
    assert body["status_ids"] == ["100", "101"]
    assert body["rule_ids"] == ["3"]
    assert body["forwarded"] is True
    assert body["action_taken"] is False
    assert body["target_account"]["id"] == "2"

    async with session_factory() as s:
        rows = (await s.execute(select(Report))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_cannot_self_report(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/reports", json={"account_id": 1}, headers=_AUTH
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_report_unknown_target_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/reports", json={"account_id": 9999}, headers=_AUTH
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_report_invalid_category_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/reports",
        json={"account_id": 2, "category": "nonsense"},
        headers=_AUTH,
    )
    assert response.status_code == 422


# ---------- domain blocks ----------


@pytest.mark.asyncio
async def test_domain_blocks_crud(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    empty = await client.get("/api/v1/domain_blocks", headers=_AUTH)
    assert empty.json() == []

    add = await client.post(
        "/api/v1/domain_blocks?domain=spam.social", headers=_AUTH
    )
    assert add.status_code == 200

    listed = await client.get("/api/v1/domain_blocks", headers=_AUTH)
    assert listed.json() == ["spam.social"]

    # Idempotent add: no duplicate row.
    await client.post(
        "/api/v1/domain_blocks?domain=spam.social", headers=_AUTH
    )
    async with session_factory() as s:
        rows = (await s.execute(select(AccountDomainBlock))).scalars().all()
        assert len(rows) == 1

    removed = await client.request(
        "DELETE",
        "/api/v1/domain_blocks?domain=spam.social",
        headers=_AUTH,
    )
    assert removed.status_code == 200
    async with session_factory() as s:
        rows = (await s.execute(select(AccountDomainBlock))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_domain_block_filters_home_timeline(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice follows eve (remote, domain=spam.social) and bob (local).
    Domain-blocking spam.social hides eve's posts from home."""
    await _seed(session_factory, seed_data)
    await client.post("/api/v1/accounts/2/follow", headers=_AUTH)
    await client.post("/api/v1/accounts/3/follow", headers=_AUTH)

    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=100, account_id=2, text="from bob"))
        s.add(seed_data["make_status"](id_=101, account_id=3, text="from eve"))
        await s.commit()

    before = await client.get("/api/v1/timelines/home", headers=_AUTH)
    assert sorted(row["id"] for row in before.json()) == ["100", "101"]

    await client.post(
        "/api/v1/domain_blocks?domain=spam.social", headers=_AUTH
    )

    after = await client.get("/api/v1/timelines/home", headers=_AUTH)
    assert [row["id"] for row in after.json()] == ["100"]


@pytest.mark.asyncio
async def test_domain_block_blank_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post("/api/v1/domain_blocks?domain=", headers=_AUTH)
    assert response.status_code == 422


# ---------- suggestions ----------


@pytest.mark.asyncio
async def test_suggestions_returns_empty(client: AsyncClient) -> None:
    response = await client.get("/api/v1/suggestions")
    assert response.status_code == 200
    assert response.json() == []

    v2 = await client.get("/api/v2/suggestions")
    assert v2.status_code == 200
    assert v2.json() == []
