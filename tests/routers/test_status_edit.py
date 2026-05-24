"""Tests for PUT /statuses/{id} + /source + /history."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Notification, Status, StatusEdit

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
                seed_data["make_status"](
                    id_=100, account_id=1, text="hello", spoiler_text=""
                ),
                seed_data["make_status_stat"](status_id=100),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_edit_requires_auth(client: AsyncClient) -> None:
    response = await client.put(
        "/api/v1/statuses/100", json={"status": "hi"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_edit_bumps_edited_at_and_snapshots_history(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    response = await client.put(
        "/api/v1/statuses/100",
        json={"status": "hello world (corrected)"},
        headers=_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "<p>hello world (corrected)</p>"
    assert body["edited_at"] is not None

    async with session_factory() as s:
        row = (await s.execute(select(Status).where(Status.id == 100))).scalar_one()
        assert row.text == "hello world (corrected)"
        assert row.edited_at is not None
        snapshots = (
            await s.execute(select(StatusEdit).where(StatusEdit.status_id == 100))
        ).scalars().all()
        assert len(snapshots) == 1
        assert snapshots[0].text == "hello"  # the previous state


@pytest.mark.asyncio
async def test_edit_non_author_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Status 200 is bob's. Alice can't edit it."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=200, account_id=2, text="bob's"))
        s.add(seed_data["make_status_stat"](status_id=200))
        await s.commit()

    response = await client.put(
        "/api/v1/statuses/200",
        json={"status": "hostile takeover"},
        headers=_AUTH,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_edit_with_no_actual_change_is_noop(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.put(
        "/api/v1/statuses/100",
        json={"status": "hello"},  # same text
        headers=_AUTH,
    )
    assert response.status_code == 200
    # No edit snapshot when nothing actually changed.
    async with session_factory() as s:
        snapshots = (
            await s.execute(select(StatusEdit).where(StatusEdit.status_id == 100))
        ).scalars().all()
        assert snapshots == []
        row = (await s.execute(select(Status).where(Status.id == 100))).scalar_one()
        assert row.edited_at is None


@pytest.mark.asyncio
async def test_edit_spoiler_implies_sensitive(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.put(
        "/api/v1/statuses/100",
        json={"status": "hello", "spoiler_text": "warning"},
        headers=_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sensitive"] is True
    assert body["spoiler_text"] == "warning"


@pytest.mark.asyncio
async def test_source_returns_raw_text(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get("/api/v1/statuses/100/source", headers=_AUTH)
    assert response.status_code == 200
    assert response.json() == {"id": "100", "text": "hello", "spoiler_text": ""}


@pytest.mark.asyncio
async def test_source_non_author_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(seed_data["make_status"](id_=300, account_id=2, text="bob's"))
        await s.commit()

    response = await client.get("/api/v1/statuses/300/source", headers=_AUTH)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_history_returns_snapshots_plus_current(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.put(
        "/api/v1/statuses/100", json={"status": "v2"}, headers=_AUTH
    )
    await client.put(
        "/api/v1/statuses/100", json={"status": "v3"}, headers=_AUTH
    )

    response = await client.get("/api/v1/statuses/100/history", headers=_AUTH)
    assert response.status_code == 200
    body = response.json()
    contents = [row["content"] for row in body]
    assert contents == ["<p>hello</p>", "<p>v2</p>", "<p>v3</p>"]


@pytest.mark.asyncio
async def test_edit_notifies_local_rebloggers(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Local user who reblogged a status gets an `update` notification when it's edited."""
    await _seed(session_factory, seed_data)
    # bob has a user row so he's local
    async with session_factory() as s:
        s.add(seed_data["make_user"](id_=2, account_id=2))
        # bob reblogged alice's status 100
        s.add(seed_data["make_status"](id_=200, account_id=2, reblog_of_id=100))
        s.add(seed_data["make_status_stat"](status_id=200))
        await s.commit()

    # alice edits her original status
    edit_resp = await client.put(
        "/api/v1/statuses/100", json={"status": "updated text"}, headers=_AUTH
    )
    assert edit_resp.status_code == 200

    async with session_factory() as s:
        notifs = (
            await s.execute(
                select(Notification).where(
                    Notification.account_id == 2,
                    Notification.type == "update",
                )
            )
        ).scalars().all()
    assert len(notifs) == 1
    assert notifs[0].from_account_id == 1
    assert notifs[0].activity_id == 100
