"""Tests for POST /api/v1/statuses (create) and DELETE /api/v1/statuses/{id}."""

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
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account_stat"](account_id=1, statuses_count=0),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_create_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/statuses", json={"status": "hello"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_minimum_post(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={"status": "hello world"},
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["content"] == "<p>hello world</p>"
    assert body["visibility"] == "public"
    assert body["sensitive"] is False
    assert body["account"]["id"] == "1"
    assert body["uri"].endswith(f"/users/alice/statuses/{body['id']}")
    assert body["url"].endswith(f"/@alice/{body['id']}")

    async with session_factory() as s:
        rows = (await s.execute(select(Status))).scalars().all()
        assert len(rows) == 1
        stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        assert stat.statuses_count == 1
        assert stat.last_status_at is not None


@pytest.mark.asyncio
async def test_create_with_visibility_and_spoiler(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={
            "status": "cw test",
            "visibility": "private",
            "spoiler_text": "topic",
        },
        headers=_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["visibility"] == "private"
    assert body["spoiler_text"] == "topic"
    # spoiler_text presence implies sensitive (matches Rails preprocess_attributes!)
    assert body["sensitive"] is True


@pytest.mark.asyncio
async def test_create_rejects_empty(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={"status": "   "},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_reply_sets_thread(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # Author bob, plus a public parent status from bob.
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_status"](id_=500, account_id=2, text="parent"),
                seed_data["make_status_stat"](status_id=500),
            ]
        )
        await s.commit()

    response = await client.post(
        "/api/v1/statuses",
        json={"status": "reply!", "in_reply_to_id": 500},
        headers=_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["in_reply_to_id"] == "500"
    assert body["in_reply_to_account_id"] == "2"

    async with session_factory() as s:
        parent_stat = (
            await s.execute(select(StatusStat).where(StatusStat.status_id == 500))
        ).scalar_one()
        assert parent_stat.replies_count == 1


@pytest.mark.asyncio
async def test_create_reply_to_invisible_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_status"](id_=600, account_id=2, visibility=Visibility.DIRECT),
            ]
        )
        await s.commit()

    response = await client.post(
        "/api/v1/statuses",
        json={"status": "sneaky", "in_reply_to_id": 600},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_author_discards_and_decrements(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    created = await client.post(
        "/api/v1/statuses", json={"status": "to delete"}, headers=_AUTH
    )
    sid = created.json()["id"]

    response = await client.delete(f"/api/v1/statuses/{sid}", headers=_AUTH)
    assert response.status_code == 200

    async with session_factory() as s:
        row = (
            await s.execute(select(Status).where(Status.id == int(sid)))
        ).scalar_one()
        assert row.deleted_at is not None
        stat = (
            await s.execute(select(AccountStat).where(AccountStat.account_id == 1))
        ).scalar_one()
        assert stat.statuses_count == 0


@pytest.mark.asyncio
async def test_delete_non_author_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account_stat"](account_id=2, statuses_count=1),
                seed_data["make_status"](id_=700, account_id=2, text="bob's post"),
                seed_data["make_status_stat"](status_id=700),
            ]
        )
        await s.commit()

    response = await client.delete("/api/v1/statuses/700", headers=_AUTH)
    assert response.status_code == 404

    async with session_factory() as s:
        row = (await s.execute(select(Status).where(Status.id == 700))).scalar_one()
        assert row.deleted_at is None
