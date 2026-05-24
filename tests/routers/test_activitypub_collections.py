"""Tests for `/users/{username}/{followers,following}` AP collections."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.common.snowflake import now_id
from app.python.models import Follow, Visibility


async def _seed_alice(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    hide_collections: bool = False,
) -> int:
    """Seed local alice; returns her account id."""
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=1,
                username="alice",
                domain=None,
                hide_collections=hide_collections,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=1))
        await s.commit()
    return 1


async def _seed_remote_follower(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    id_: int,
    username: str,
    domain: str,
    uri: str,
) -> int:
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=id_, username=username, domain=domain, uri=uri
            )
        )
        s.add(seed_data["make_account_stat"](account_id=id_))
        await s.commit()
    return id_


async def _seed_follow(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    target_account_id: int,
) -> None:
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    async with session_factory() as s:
        s.add(
            Follow(
                id=now_id(),
                account_id=account_id,
                target_account_id=target_account_id,
                show_reblogs=True,
                notify=False,
                languages=None,
                uri=None,
                created_at=now,
                updated_at=now,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_followers_collection_root_returns_total_and_first(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    alice_id = await _seed_alice(session_factory, seed_data)
    for i in range(3):
        follower_id = await _seed_remote_follower(
            session_factory, seed_data,
            id_=100 + i, username=f"f{i}",
            domain="other.test", uri=f"https://other.test/users/f{i}",
        )
        await _seed_follow(
            session_factory, account_id=follower_id, target_account_id=alice_id
        )

    response = await client.get("/users/alice/followers")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/activity+json"
    )
    body = response.json()
    assert body["type"] == "OrderedCollection"
    assert body["id"].endswith("/users/alice/followers")
    assert body["totalItems"] == 3
    assert body["first"].endswith("/users/alice/followers?page=1")


@pytest.mark.asyncio
async def test_followers_page_lists_actor_uris(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    alice_id = await _seed_alice(session_factory, seed_data)
    expected_uris: list[str] = []
    for i in range(3):
        uri = f"https://other.test/users/f{i}"
        follower_id = await _seed_remote_follower(
            session_factory, seed_data,
            id_=100 + i, username=f"f{i}",
            domain="other.test", uri=uri,
        )
        await _seed_follow(
            session_factory, account_id=follower_id, target_account_id=alice_id
        )
        expected_uris.append(uri)

    response = await client.get("/users/alice/followers?page=1")
    body = response.json()
    assert body["type"] == "OrderedCollectionPage"
    assert body["partOf"].endswith("/users/alice/followers")
    assert body["totalItems"] == 3
    assert set(body["orderedItems"]) == set(expected_uris)
    # Page 1, fewer-than-PAGE_SIZE items → no `next` link.
    assert "next" not in body
    # Page 1 has no `prev` either.
    assert "prev" not in body


@pytest.mark.asyncio
async def test_followers_page_emits_next_link_when_full(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A full page (40 items) must include `next` so crawlers continue."""
    alice_id = await _seed_alice(session_factory, seed_data)
    for i in range(40):
        follower_id = await _seed_remote_follower(
            session_factory, seed_data,
            id_=200 + i, username=f"u{i}",
            domain="other.test", uri=f"https://other.test/users/u{i}",
        )
        await _seed_follow(
            session_factory, account_id=follower_id, target_account_id=alice_id
        )

    response = await client.get("/users/alice/followers?page=1")
    body = response.json()
    assert len(body["orderedItems"]) == 40
    assert body["next"].endswith("?page=2")


@pytest.mark.asyncio
async def test_following_collection_returns_targets(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Alice's `following` collection lists who she follows (target_account_id)."""
    alice_id = await _seed_alice(session_factory, seed_data)
    for i in range(2):
        target_id = await _seed_remote_follower(
            session_factory, seed_data,
            id_=300 + i, username=f"t{i}",
            domain="other.test", uri=f"https://other.test/users/t{i}",
        )
        await _seed_follow(
            session_factory, account_id=alice_id, target_account_id=target_id
        )

    response = await client.get("/users/alice/following")
    assert response.json()["totalItems"] == 2

    page = await client.get("/users/alice/following?page=1")
    body = page.json()
    assert set(body["orderedItems"]) == {
        "https://other.test/users/t0",
        "https://other.test/users/t1",
    }


@pytest.mark.asyncio
async def test_hide_collections_zeroes_total_and_skips_items(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """`hide_collections=true` → totalItems=0 even when followers exist.

    Privacy contract: peers see the collection URL but can't enumerate
    who's on it. Page requests come back empty too.
    """
    alice_id = await _seed_alice(
        session_factory, seed_data, hide_collections=True
    )
    follower_id = await _seed_remote_follower(
        session_factory, seed_data,
        id_=400, username="snoop",
        domain="other.test", uri="https://other.test/users/snoop",
    )
    await _seed_follow(
        session_factory, account_id=follower_id, target_account_id=alice_id
    )

    response = await client.get("/users/alice/followers")
    body = response.json()
    assert body["totalItems"] == 0
    # Page request also collapses to the zero-count root — no items leak.
    page = await client.get("/users/alice/followers?page=1")
    assert page.json()["totalItems"] == 0
    assert "orderedItems" not in page.json()


# ---------- Outbox ----------


async def _seed_status(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    status_id: int,
    account_id: int,
    visibility: Visibility = Visibility.PUBLIC,
    deleted: bool = False,
    reblog_of_id: int | None = None,
    text: str = "hi",
) -> None:
    from datetime import datetime

    async with session_factory() as s:
        s.add(
            seed_data["make_status"](
                id_=status_id,
                account_id=account_id,
                text=text,
                visibility=visibility,
                reblog_of_id=reblog_of_id,
                deleted_at=datetime.now() if deleted else None,
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_outbox_root_counts_public_and_unlisted(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    alice_id = await _seed_alice(session_factory, seed_data)
    await _seed_status(
        session_factory, seed_data,
        status_id=1, account_id=alice_id, visibility=Visibility.PUBLIC,
    )
    await _seed_status(
        session_factory, seed_data,
        status_id=2, account_id=alice_id, visibility=Visibility.UNLISTED,
    )
    # PRIVATE + DIRECT excluded.
    await _seed_status(
        session_factory, seed_data,
        status_id=3, account_id=alice_id, visibility=Visibility.PRIVATE,
    )
    await _seed_status(
        session_factory, seed_data,
        status_id=4, account_id=alice_id, visibility=Visibility.DIRECT,
    )
    # Soft-deleted excluded.
    await _seed_status(
        session_factory, seed_data,
        status_id=5, account_id=alice_id, visibility=Visibility.PUBLIC,
        deleted=True,
    )
    # Reblog excluded — Mastodon emits Announce activities separately.
    await _seed_status(
        session_factory, seed_data,
        status_id=6, account_id=alice_id, visibility=Visibility.PUBLIC,
        reblog_of_id=1,
    )

    response = await client.get("/users/alice/outbox")
    body = response.json()
    assert body["type"] == "OrderedCollection"
    assert body["totalItems"] == 2
    assert body["first"].endswith("/users/alice/outbox?page=1")


@pytest.mark.asyncio
async def test_outbox_page_lists_create_activities(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    alice_id = await _seed_alice(session_factory, seed_data)
    await _seed_status(
        session_factory, seed_data,
        status_id=1, account_id=alice_id, text="hello world",
    )
    response = await client.get("/users/alice/outbox?page=1")
    body = response.json()
    assert body["type"] == "OrderedCollectionPage"
    assert body["totalItems"] == 1
    assert len(body["orderedItems"]) == 1
    activity = body["orderedItems"][0]
    assert activity["type"] == "Create"
    assert activity["actor"].endswith("/users/alice")
    assert activity["object"]["type"] == "Note"
    assert activity["object"]["content"] == "hello world"


@pytest.mark.asyncio
async def test_outbox_orders_newest_first(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    alice_id = await _seed_alice(session_factory, seed_data)
    await _seed_status(
        session_factory, seed_data,
        status_id=1, account_id=alice_id, text="first",
    )
    await _seed_status(
        session_factory, seed_data,
        status_id=2, account_id=alice_id, text="second",
    )
    await _seed_status(
        session_factory, seed_data,
        status_id=3, account_id=alice_id, text="third",
    )
    response = await client.get("/users/alice/outbox?page=1")
    items = response.json()["orderedItems"]
    assert [i["object"]["content"] for i in items] == ["third", "second", "first"]


@pytest.mark.asyncio
async def test_outbox_honors_hide_collections(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    alice_id = await _seed_alice(
        session_factory, seed_data, hide_collections=True
    )
    await _seed_status(
        session_factory, seed_data,
        status_id=1, account_id=alice_id, visibility=Visibility.PUBLIC,
    )
    response = await client.get("/users/alice/outbox")
    assert response.json()["totalItems"] == 0


@pytest.mark.asyncio
async def test_outbox_404_for_unknown_user(client: AsyncClient) -> None:
    response = await client.get("/users/nobody/outbox")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_collection_404_for_unknown_user(client: AsyncClient) -> None:
    response = await client.get("/users/nobody/followers")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_collection_404_for_remote_lookalike(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A remote alice@other.test in our DB doesn't get her followers
    collection served from our origin."""
    await _seed_remote_follower(
        session_factory, seed_data,
        id_=500, username="alice", domain="other.test",
        uri="https://other.test/users/alice",
    )
    response = await client.get("/users/alice/followers")
    assert response.status_code == 404
