"""Tests for /accounts/{id}/note + /accounts/familiar_followers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import AccountNote, Follow


_AUTH = {"Authorization": "Bearer raw-token-abc"}


def _make_follow(follow_id: int, *, follower: int, target: int) -> Follow:
    ts = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    return Follow(
        id=follow_id,
        account_id=follower,
        target_account_id=target,
        show_reblogs=True,
        notify=False,
        languages=None,
        uri=None,
        created_at=ts,
        updated_at=ts,
    )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """alice (1, viewer), bob (2), carol (3), dave (4), eve (5)."""
    async with session_factory() as s:
        s.add_all(
            [
                seed_data["make_account"](id_=1, username="alice"),
                seed_data["make_account"](id_=2, username="bob"),
                seed_data["make_account"](id_=3, username="carol"),
                seed_data["make_account"](id_=4, username="dave"),
                seed_data["make_account"](id_=5, username="eve"),
                seed_data["make_account_stat"](account_id=1),
                seed_data["make_account_stat"](account_id=2),
                seed_data["make_account_stat"](account_id=3),
                seed_data["make_account_stat"](account_id=4),
                seed_data["make_account_stat"](account_id=5),
                seed_data["make_user"](id_=1, account_id=1),
                seed_data["make_application"](),
                seed_data["make_token"](),
            ]
        )
        await s.commit()


# ---------- account notes ----------


@pytest.mark.asyncio
async def test_note_upsert_surfaces_in_relationship(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)

    response = await client.post(
        "/api/v1/accounts/2/note",
        json={"comment": "met at PyCon"},
        headers=_AUTH,
    )
    assert response.status_code == 200
    assert response.json()["note"] == "met at PyCon"

    rels = await client.get(
        "/api/v1/accounts/relationships?id[]=2", headers=_AUTH
    )
    assert rels.json()[0]["note"] == "met at PyCon"

    async with session_factory() as s:
        rows = (await s.execute(select(AccountNote))).scalars().all()
        assert len(rows) == 1
        assert rows[0].comment == "met at PyCon"


@pytest.mark.asyncio
async def test_note_update_replaces_existing(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/accounts/2/note",
        json={"comment": "first"},
        headers=_AUTH,
    )
    await client.post(
        "/api/v1/accounts/2/note",
        json={"comment": "second"},
        headers=_AUTH,
    )

    async with session_factory() as s:
        rows = (await s.execute(select(AccountNote))).scalars().all()
        assert len(rows) == 1
        assert rows[0].comment == "second"


@pytest.mark.asyncio
async def test_empty_note_deletes(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    await client.post(
        "/api/v1/accounts/2/note", json={"comment": "x"}, headers=_AUTH
    )
    cleared = await client.post(
        "/api/v1/accounts/2/note", json={"comment": ""}, headers=_AUTH
    )
    assert cleared.status_code == 200
    assert cleared.json()["note"] == ""

    async with session_factory() as s:
        rows = (await s.execute(select(AccountNote))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_note_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/accounts/2/note", json={"comment": "x"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_note_unknown_target_is_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.post(
        "/api/v1/accounts/9999/note",
        json={"comment": "x"},
        headers=_AUTH,
    )
    assert response.status_code == 404


# ---------- familiar followers ----------


@pytest.mark.asyncio
async def test_familiar_followers_intersects_follow_graph(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice follows carol + dave. carol + dave both follow bob. eve also follows
    bob but alice doesn't follow eve. Familiar followers of bob (from alice's
    perspective) = {carol, dave}."""
    await _seed(session_factory, seed_data)
    async with session_factory() as s:
        s.add(_make_follow(10, follower=1, target=3))  # alice → carol
        s.add(_make_follow(11, follower=1, target=4))  # alice → dave
        s.add(_make_follow(20, follower=3, target=2))  # carol → bob
        s.add(_make_follow(21, follower=4, target=2))  # dave → bob
        s.add(_make_follow(22, follower=5, target=2))  # eve → bob
        await s.commit()

    response = await client.get(
        "/api/v1/accounts/familiar_followers?id[]=2", headers=_AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == "2"
    usernames = sorted(a["username"] for a in body[0]["accounts"])
    assert usernames == ["carol", "dave"]


@pytest.mark.asyncio
async def test_familiar_followers_self_target_empty(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    await _seed(session_factory, seed_data)
    response = await client.get(
        "/api/v1/accounts/familiar_followers?id[]=1", headers=_AUTH
    )
    assert response.json()[0]["accounts"] == []


@pytest.mark.asyncio
async def test_familiar_followers_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/accounts/familiar_followers?id[]=2")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_familiar_followers_empty_for_no_overlap(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice follows nobody → no familiar followers anywhere."""
    await _seed(session_factory, seed_data)
    response = await client.get(
        "/api/v1/accounts/familiar_followers?id[]=2&id[]=3", headers=_AUTH
    )
    body = response.json()
    assert [r["id"] for r in body] == ["2", "3"]
    assert all(r["accounts"] == [] for r in body)
