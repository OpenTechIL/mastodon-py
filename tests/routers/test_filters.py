"""Tests for /api/v2/filters CRUD + filter application on timelines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import CustomFilter, CustomFilterKeyword, CustomFilterStatus

_AUTH = {"Authorization": "Bearer raw-token-abc"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


# ---------- CRUD ----------


@pytest.mark.asyncio
async def test_filter_index_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v2/filters")).status_code == 401


@pytest.mark.asyncio
async def test_filter_crud_round_trip(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    create = await client.post(
        "/api/v2/filters",
        json={
            "title": "no spoilers",
            "context": ["home", "public"],
            "filter_action": "hide",
            "expires_in": 3600,
        },
        headers=_AUTH,
    )
    assert create.status_code == 200
    body = create.json()
    fid = body["id"]
    assert body["title"] == "no spoilers"
    assert body["filter_action"] == "hide"
    assert sorted(body["context"]) == ["home", "public"]
    assert body["expires_at"] is not None

    updated = await client.put(
        f"/api/v2/filters/{fid}",
        json={"title": "renamed", "filter_action": "warn"},
        headers=_AUTH,
    )
    assert updated.json()["title"] == "renamed"
    assert updated.json()["filter_action"] == "warn"

    assert (await client.delete(f"/api/v2/filters/{fid}", headers=_AUTH)).status_code == 200

    async with session_factory() as s:
        assert (await s.execute(select(CustomFilter))).scalars().all() == []


@pytest.mark.asyncio
async def test_invalid_context_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v2/filters",
        json={"title": "bad", "context": ["bogus"], "filter_action": "warn"},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_invalid_action_is_422(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v2/filters",
        json={"title": "bad", "context": ["home"], "filter_action": "explode"},
        headers=_AUTH,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_keyword_subresource(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    create = await client.post(
        "/api/v2/filters",
        json={"title": "kw test", "context": ["home"], "filter_action": "warn"},
        headers=_AUTH,
    )
    fid = create.json()["id"]

    added = await client.post(
        f"/api/v2/filters/{fid}/keywords",
        json={"keyword": "spoiler", "whole_word": False},
        headers=_AUTH,
    )
    assert added.status_code == 200
    kw_id = added.json()["id"]
    assert added.json()["whole_word"] is False

    listed = await client.get(f"/api/v2/filters/{fid}/keywords", headers=_AUTH)
    assert len(listed.json()) == 1

    removed = await client.delete(
        f"/api/v2/filters/{fid}/keywords/{kw_id}", headers=_AUTH
    )
    assert removed.status_code == 200

    async with session_factory() as s:
        assert (await s.execute(select(CustomFilterKeyword))).scalars().all() == []


# ---------- Filter application ----------


async def _make_filter(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    contexts: list[str],
    keyword: str,
    whole_word: bool = True,
    expires_at=None,
    action: int = 0,
) -> int:
    """Helper: insert a CustomFilter + one keyword directly. Returns filter id."""
    ts = datetime.now(tz=UTC).replace(tzinfo=None)
    async with session_factory() as s:
        from app.python.common.snowflake import now_id

        fid = now_id()
        s.add(
            CustomFilter(
                id=fid,
                account_id=account_id,
                action=action,
                context=contexts,
                expires_at=expires_at,
                phrase="test",
                created_at=ts,
                updated_at=ts,
            )
        )
        s.add(
            CustomFilterKeyword(
                id=now_id(),
                custom_filter_id=fid,
                keyword=keyword,
                whole_word=whole_word,
                created_at=ts,
                updated_at=ts,
            )
        )
        await s.commit()
    return fid


@pytest.mark.asyncio
async def test_filter_marks_matching_status_on_home(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await _make_filter(
        session_factory,
        account_id=1,
        contexts=["home"],
        keyword="banana",
    )
    await client.post(
        "/api/v1/statuses", json={"status": "I love banana!"}, headers=_AUTH
    )
    await client.post(
        "/api/v1/statuses", json={"status": "Apples only here."}, headers=_AUTH
    )

    response = await client.get("/api/v1/timelines/home", headers=_AUTH)
    body = response.json()
    by_text = {row["content"]: row for row in body}
    assert by_text["<p>I love banana!</p>"]["filtered"]
    assert by_text["<p>I love banana!</p>"]["filtered"][0]["keyword_matches"] == ["banana"]
    assert by_text["<p>Apples only here.</p>"]["filtered"] == []


@pytest.mark.asyncio
async def test_filter_does_not_apply_to_wrong_context(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    # Filter only covers "home" — public timeline should NOT mark it.
    await _make_filter(
        session_factory,
        account_id=1,
        contexts=["home"],
        keyword="banana",
    )
    await client.post(
        "/api/v1/statuses", json={"status": "banana republic"}, headers=_AUTH
    )

    public = await client.get("/api/v1/timelines/public", headers=_AUTH)
    assert public.json()[0]["filtered"] == []


@pytest.mark.asyncio
async def test_expired_filter_is_ignored(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    yesterday = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(days=1)
    await _make_filter(
        session_factory,
        account_id=1,
        contexts=["home"],
        keyword="banana",
        expires_at=yesterday,
    )
    await client.post(
        "/api/v1/statuses", json={"status": "banana split"}, headers=_AUTH
    )

    response = await client.get("/api/v1/timelines/home", headers=_AUTH)
    assert response.json()[0]["filtered"] == []


@pytest.mark.asyncio
async def test_whole_word_vs_substring(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await _make_filter(
        session_factory,
        account_id=1,
        contexts=["home"],
        keyword="cat",
        whole_word=True,
    )
    await client.post(
        "/api/v1/statuses", json={"status": "my cat is great"}, headers=_AUTH
    )
    # "catastrophe" contains "cat" but not as a whole word
    await client.post(
        "/api/v1/statuses", json={"status": "catastrophe averted"}, headers=_AUTH
    )

    response = await client.get("/api/v1/timelines/home", headers=_AUTH)
    by_text = {row["content"]: row for row in response.json()}
    assert by_text["<p>my cat is great</p>"]["filtered"]  # matched
    assert by_text["<p>catastrophe averted</p>"]["filtered"] == []  # not matched


@pytest.mark.asyncio
async def test_explicit_status_match(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    fid = await _make_filter(
        session_factory,
        account_id=1,
        contexts=["home"],
        keyword="never-matches-anything",  # keep keyword non-matching so only status_id triggers
    )
    posted = await client.post(
        "/api/v1/statuses", json={"status": "completely innocuous"}, headers=_AUTH
    )
    sid = int(posted.json()["id"])

    # Mark the specific status as filtered.
    async with session_factory() as s:
        from app.python.common.snowflake import now_id

        ts = datetime.now(tz=UTC).replace(tzinfo=None)
        s.add(
            CustomFilterStatus(
                id=now_id(),
                custom_filter_id=fid,
                status_id=sid,
                created_at=ts,
                updated_at=ts,
            )
        )
        await s.commit()

    response = await client.get("/api/v1/timelines/home", headers=_AUTH)
    body = response.json()
    filtered = body[0]["filtered"]
    assert filtered
    assert filtered[0]["status_matches"] == [str(sid)]
    assert filtered[0]["keyword_matches"] == []
