"""Tests for poll creation + voting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Poll, PollVote


_AUTH = {"Authorization": "Bearer raw-token-abc"}
_BOB_TOKEN = "bob-token"
_BOB_AUTH = {"Authorization": f"Bearer {_BOB_TOKEN}"}


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
                seed_data["make_user"](id_=2, account_id=2, email="bob@example.com"),
                seed_data["make_application"](),
                seed_data["make_token"](id_=1, token="raw-token-abc", resource_owner_id=1),
                seed_data["make_token"](id_=2, token=_BOB_TOKEN, resource_owner_id=2),
            ]
        )
        await s.commit()


# ---------- creation ----------


@pytest.mark.asyncio
async def test_post_with_poll_creates_row(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={
            "status": "favourite color?",
            "poll": {
                "options": ["red", "blue", "green"],
                "expires_in": 3600,
                "multiple": False,
            },
        },
        headers=_AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["poll"] is not None
    assert [opt["title"] for opt in body["poll"]["options"]] == ["red", "blue", "green"]
    assert body["poll"]["multiple"] is False
    assert body["poll"]["expired"] is False

    async with session_factory() as s:
        rows = (await s.execute(select(Poll))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_poll_with_one_option_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={
            "status": "broken poll",
            "poll": {"options": ["only one"]},
        },
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_poll_with_duplicate_options_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/statuses",
        json={
            "status": "dupe poll",
            "poll": {"options": ["red", "red", "blue"]},
        },
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_poll_and_media_together_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Mastodon disallows attaching both media and a poll."""
    await _seed(session_factory, seed_data)
    import io as _io

    from PIL import Image as _Image

    _buf = _io.BytesIO()
    _Image.new("RGB", (16, 16), (0, 0, 0)).save(_buf, format="PNG")
    png = _buf.getvalue()
    upload = await client.post(
        "/api/v1/media",
        files={"file": ("a.png", png, "image/png")},
        headers=_AUTH,
    )
    mid = int(upload.json()["id"])
    response = await client.post(
        "/api/v1/statuses",
        json={
            "status": "both",
            "media_ids": [mid],
            "poll": {"options": ["a", "b"]},
        },
        headers=_AUTH,
    )
    assert response.status_code == 422


# ---------- voting ----------


@pytest.mark.asyncio
async def test_vote_increments_tallies(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={
            "status": "poll!",
            "poll": {"options": ["red", "blue"], "expires_in": 3600},
        },
        headers=_AUTH,
    )
    poll_id = posted.json()["poll"]["id"]

    response = await client.post(
        f"/api/v1/polls/{poll_id}/votes",
        json={"choices": [1]},
        headers=_BOB_AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["voted"] is True
    assert body["own_votes"] == [1]
    counts = [opt["votes_count"] for opt in body["options"]]
    assert counts == [0, 1]
    assert body["votes_count"] == 1


@pytest.mark.asyncio
async def test_single_choice_rejects_multiple_choices(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={
            "status": "poll!",
            "poll": {"options": ["a", "b", "c"], "multiple": False},
        },
        headers=_AUTH,
    )
    pid = posted.json()["poll"]["id"]

    response = await client.post(
        f"/api/v1/polls/{pid}/votes",
        json={"choices": [0, 1]},
        headers=_BOB_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_multiple_choice_allows_multiple(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={
            "status": "poll!",
            "poll": {"options": ["a", "b", "c"], "multiple": True},
        },
        headers=_AUTH,
    )
    pid = posted.json()["poll"]["id"]

    response = await client.post(
        f"/api/v1/polls/{pid}/votes",
        json={"choices": [0, 2]},
        headers=_BOB_AUTH,
    )
    assert response.status_code == 200
    assert sorted(response.json()["own_votes"]) == [0, 2]
    assert response.json()["votes_count"] == 2


@pytest.mark.asyncio
async def test_invalid_choice_index_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "poll!", "poll": {"options": ["a", "b"]}},
        headers=_AUTH,
    )
    pid = posted.json()["poll"]["id"]
    response = await client.post(
        f"/api/v1/polls/{pid}/votes",
        json={"choices": [99]},
        headers=_BOB_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_expired_poll_rejects_vote(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    naive_now,
) -> None:
    await _seed(session_factory, seed_data)
    # Insert a Poll directly with an expired timestamp; goes around
    # post_status's `expires_in >= 300` check.
    async with session_factory() as s:
        from app.python.common.snowflake import now_id

        s.add(seed_data["make_status"](id_=500, account_id=1, text="poll"))
        s.add(seed_data["make_status_stat"](status_id=500))
        s.add(
            Poll(
                id=now_id(),
                account_id=1,
                status_id=500,
                options=["a", "b"],
                cached_tallies=[0, 0],
                multiple=False,
                hide_totals=False,
                expires_at=naive_now - timedelta(hours=1),
                votes_count=0,
                voters_count=None,
                lock_version=0,
                created_at=naive_now,
                updated_at=naive_now,
            )
        )
        await s.commit()

    show = await client.get("/api/v1/statuses/500")
    poll_id = show.json()["poll"]["id"]

    response = await client.post(
        f"/api/v1/polls/{poll_id}/votes",
        json={"choices": [0]},
        headers=_BOB_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_double_voting_rejected(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "poll!", "poll": {"options": ["a", "b"]}},
        headers=_AUTH,
    )
    pid = posted.json()["poll"]["id"]

    first = await client.post(
        f"/api/v1/polls/{pid}/votes", json={"choices": [0]}, headers=_BOB_AUTH
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/polls/{pid}/votes", json={"choices": [1]}, headers=_BOB_AUTH
    )
    assert second.status_code == 422


@pytest.mark.asyncio
async def test_show_poll_reflects_own_votes(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "poll!", "poll": {"options": ["a", "b"]}},
        headers=_AUTH,
    )
    pid = posted.json()["poll"]["id"]
    await client.post(
        f"/api/v1/polls/{pid}/votes", json={"choices": [1]}, headers=_BOB_AUTH
    )

    response = await client.get(f"/api/v1/polls/{pid}", headers=_BOB_AUTH)
    body = response.json()
    assert body["voted"] is True
    assert body["own_votes"] == [1]

    anon = await client.get(f"/api/v1/polls/{pid}")
    assert anon.json()["voted"] is False
    assert anon.json()["own_votes"] == []


@pytest.mark.asyncio
async def test_hide_totals_until_voted(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={
            "status": "secret poll",
            "poll": {"options": ["a", "b"], "hide_totals": True},
        },
        headers=_AUTH,
    )
    pid = posted.json()["poll"]["id"]

    # Counts hidden while poll is open + viewer hasn't voted.
    show = await client.get(f"/api/v1/polls/{pid}")
    assert show.json()["options"][0]["votes_count"] is None


@pytest.mark.asyncio
async def test_vote_requires_auth(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    posted = await client.post(
        "/api/v1/statuses",
        json={"status": "poll!", "poll": {"options": ["a", "b"]}},
        headers=_AUTH,
    )
    pid = posted.json()["poll"]["id"]

    response = await client.post(
        f"/api/v1/polls/{pid}/votes", json={"choices": [0]}
    )
    assert response.status_code == 401
