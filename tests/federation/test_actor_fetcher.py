"""Tests for `fetch_and_persist_actor`."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.federation.actor_fetcher import fetch_and_persist_actor
from app.python.models import Account

_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0\n"
    "-----END PUBLIC KEY-----\n"
)


def _actor_json(actor_url: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": actor_url,
        "type": "Person",
        "preferredUsername": "carol",
        "name": "Carol",
        "summary": "<p>hi</p>",
        "url": "https://other.test/@carol",
        "inbox": f"{actor_url}/inbox",
        "endpoints": {"sharedInbox": "https://other.test/inbox"},
        "publicKey": {
            "id": f"{actor_url}#main-key",
            "owner": actor_url,
            "publicKeyPem": _PEM,
        },
        "manuallyApprovesFollowers": False,
        "discoverable": True,
        "indexable": True,
    }
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_fetch_inserts_new_account(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor_url = "https://other.test/users/carol"
    async with respx.mock() as router:
        router.get(actor_url).respond(json=_actor_json(actor_url))
        async with session_factory() as s, httpx.AsyncClient() as client:
            row = await fetch_and_persist_actor(s, client, actor_url)
        assert row is not None
        assert row.username == "carol"
        assert row.domain == "other.test"
        assert row.uri == actor_url
        assert row.public_key == _PEM
        assert row.inbox_url == f"{actor_url}/inbox"
        assert row.shared_inbox_url == "https://other.test/inbox"
        assert row.actor_type == "Person"


@pytest.mark.asyncio
async def test_fetch_returns_existing_without_http(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """Already-known actor → no HTTP call."""
    actor_url = "https://other.test/users/carol"
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=1, username="carol", domain="other.test",
                uri=actor_url, public_key=_PEM,
            )
        )
        s.add(seed_data["make_account_stat"](account_id=1))
        await s.commit()

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(actor_url)
        async with session_factory() as s, httpx.AsyncClient() as client:
            row = await fetch_and_persist_actor(s, client, actor_url)
        assert row is not None
        assert row.id == 1
        assert not route.called


@pytest.mark.asyncio
async def test_fetch_returns_none_on_404(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    actor_url = "https://other.test/users/missing"
    async with respx.mock() as router:
        router.get(actor_url).respond(status_code=404)
        async with session_factory() as s, httpx.AsyncClient() as client:
            row = await fetch_and_persist_actor(s, client, actor_url)
        assert row is None


@pytest.mark.asyncio
async def test_fetch_returns_none_when_preferredUsername_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mastodon's contract: username is mandatory. A JSON-LD dump
    without `preferredUsername` is rejected — no row created."""
    actor_url = "https://other.test/users/anon"
    async with respx.mock() as router:
        router.get(actor_url).respond(
            json={"id": actor_url, "type": "Person", "inbox": f"{actor_url}/inbox"}
        )
        async with session_factory() as s, httpx.AsyncClient() as client:
            row = await fetch_and_persist_actor(s, client, actor_url)
        assert row is None
        async with session_factory() as s:
            rows = (await s.execute(select(Account))).scalars().all()
            assert rows == []


@pytest.mark.asyncio
async def test_fetch_returns_none_on_non_http_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s, httpx.AsyncClient() as client:
        row = await fetch_and_persist_actor(s, client, "acct:user@host")
    assert row is None
