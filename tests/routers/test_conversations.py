"""Tests for /api/v1/conversations + /api/v1/timelines/direct."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import AccountConversation, Conversation


_AUTH = {"Authorization": "Bearer raw-token-abc"}
_BOB_TOKEN = "bob-token"
_BOB_AUTH = {"Authorization": f"Bearer {_BOB_TOKEN}"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """alice (1, alice-token); bob (2, bob-token); both local users."""
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_user"](id_=2, account_id=2, email="bob@example.com"),
                seed_data["make_application"](),
                seed_data["make_token"](id_=1, token="raw-token-abc", resource_owner_id=1),
                seed_data["make_token"](id_=2, token=_BOB_TOKEN, resource_owner_id=2),
            ]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_dm_creates_conversation_rows(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice DMs bob → one Conversation row + two AccountConversation
    rows (one per participant, unread true for bob, false for alice)."""
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "secret @bob", "visibility": "direct"},
        headers=_AUTH,
    )
    assert posted.status_code == 200

    async with session_factory() as s:
        convos = (await s.execute(select(Conversation))).scalars().all()
        assert len(convos) == 1
        rows = (await s.execute(select(AccountConversation))).scalars().all()
        assert len(rows) == 2
        by_account = {r.account_id: r for r in rows}
        assert by_account[1].unread is False  # author
        assert by_account[2].unread is True   # recipient
        assert by_account[1].participant_account_ids == [2]
        assert by_account[2].participant_account_ids == [1]


@pytest.mark.asyncio
async def test_conversations_index_shows_both_views(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses",
        json={"status": "hello @bob secret", "visibility": "direct"},
        headers=_AUTH,
    )

    alice_view = await client.get("/api/v1/conversations", headers=_AUTH)
    bob_view = await client.get("/api/v1/conversations", headers=_BOB_AUTH)
    assert len(alice_view.json()) == 1
    assert len(bob_view.json()) == 1
    assert alice_view.json()[0]["unread"] is False
    assert bob_view.json()[0]["unread"] is True
    # Each side's `accounts` list is the OTHER participants.
    assert [a["username"] for a in alice_view.json()[0]["accounts"]] == ["bob"]
    assert [a["username"] for a in bob_view.json()[0]["accounts"]] == ["alice"]


@pytest.mark.asyncio
async def test_mark_read_flips_unread(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses",
        json={"status": "ping @bob", "visibility": "direct"},
        headers=_AUTH,
    )

    bob_index = await client.get("/api/v1/conversations", headers=_BOB_AUTH)
    cid = bob_index.json()[0]["id"]

    read_response = await client.post(
        f"/api/v1/conversations/{cid}/read", headers=_BOB_AUTH
    )
    assert read_response.status_code == 200
    assert read_response.json()["unread"] is False


@pytest.mark.asyncio
async def test_delete_removes_only_callers_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/statuses",
        json={"status": "@bob", "visibility": "direct"},
        headers=_AUTH,
    )
    bob_index = await client.get("/api/v1/conversations", headers=_BOB_AUTH)
    cid = bob_index.json()[0]["id"]

    response = await client.delete(
        f"/api/v1/conversations/{cid}", headers=_BOB_AUTH
    )
    assert response.status_code == 200

    async with session_factory() as s:
        remaining = (await s.execute(select(AccountConversation))).scalars().all()
        # alice's view persists; bob's is gone
        assert len(remaining) == 1
        assert remaining[0].account_id == 1


@pytest.mark.asyncio
async def test_direct_timeline_returns_dms_for_both_sides(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "secret @bob", "visibility": "direct"},
        headers=_AUTH,
    )
    sid = posted.json()["id"]

    alice = await client.get("/api/v1/timelines/direct", headers=_AUTH)
    bob = await client.get("/api/v1/timelines/direct", headers=_BOB_AUTH)

    assert [s["id"] for s in alice.json()] == [sid]
    assert [s["id"] for s in bob.json()] == [sid]


@pytest.mark.asyncio
async def test_thread_replies_share_conversation(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice DMs bob, bob replies. The reply should reuse the same Conversation."""
    await _seed(session_factory, seed_data)
    first = await client.post(
        "/api/v1/statuses",
        json={"status": "hi @bob", "visibility": "direct"},
        headers=_AUTH,
    )
    first_id = first.json()["id"]

    reply = await client.post(
        "/api/v1/statuses",
        json={
            "status": "@alice hey back",
            "visibility": "direct",
            "in_reply_to_id": int(first_id),
        },
        headers=_BOB_AUTH,
    )
    assert reply.status_code == 200

    async with session_factory() as s:
        convos = (await s.execute(select(Conversation))).scalars().all()
        # Single Conversation reused.
        assert len(convos) == 1


@pytest.mark.asyncio
async def test_delete_unknown_conversation_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.delete("/api/v1/conversations/9999", headers=_AUTH)
    assert response.status_code == 404
