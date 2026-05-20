"""Tests for GET /api/v1/directory."""

from __future__ import annotations

from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.models import Account


def _make_account(
    *,
    id_: int,
    username: str,
    naive_now: datetime,
    domain: str | None = None,
    discoverable: bool = True,
    suspended: bool = False,
) -> Account:
    return Account(
        id=id_,
        username=username,
        domain=domain,
        display_name=username,
        discoverable=discoverable,
        suspended_at=None if not suspended else naive_now,
        locked=False,
        fields=[],
        created_at=naive_now,
        updated_at=naive_now,
    )


async def _seed(factory: async_sessionmaker[AsyncSession], accounts: list[Account]) -> None:
    async with factory() as s:
        s.add_all(accounts)
        await s.commit()


@pytest.mark.asyncio
async def test_directory_returns_discoverable_accounts(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    naive_now: datetime,
) -> None:
    acc1 = _make_account(id_=10_001, username="dir_alice", naive_now=naive_now)
    acc2 = _make_account(id_=10_002, username="dir_bob", naive_now=naive_now)
    acc3 = _make_account(id_=10_003, username="dir_hidden", discoverable=False, naive_now=naive_now)
    await _seed(session_factory, [acc1, acc2, acc3])

    resp = await client.get("/api/v1/directory?order=new&limit=80")
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()]
    assert str(acc1.id) in ids
    assert str(acc2.id) in ids
    assert str(acc3.id) not in ids  # not discoverable


@pytest.mark.asyncio
async def test_directory_local_filter(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    naive_now: datetime,
) -> None:
    local_acc = _make_account(id_=10_011, username="dir_local_user", domain=None, naive_now=naive_now)
    remote_acc = _make_account(id_=10_012, username="dir_remote_user", domain="other.social", naive_now=naive_now)
    await _seed(session_factory, [local_acc, remote_acc])

    resp = await client.get("/api/v1/directory?local=true&order=new&limit=80")
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()]
    assert str(local_acc.id) in ids
    assert str(remote_acc.id) not in ids


@pytest.mark.asyncio
async def test_directory_order_new(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    naive_now: datetime,
) -> None:
    acc_old = _make_account(id_=10_021, username="dir_older", naive_now=naive_now)
    acc_new = _make_account(id_=10_022, username="dir_newer", naive_now=naive_now)
    await _seed(session_factory, [acc_old, acc_new])

    resp = await client.get("/api/v1/directory?order=new&limit=80")
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()]
    if str(acc_new.id) in ids and str(acc_old.id) in ids:
        assert ids.index(str(acc_new.id)) < ids.index(str(acc_old.id))


@pytest.mark.asyncio
async def test_directory_pagination_limit(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    naive_now: datetime,
) -> None:
    accounts = [
        _make_account(id_=10_030 + i, username=f"dir_paged_{i}", naive_now=naive_now)
        for i in range(5)
    ]
    await _seed(session_factory, accounts)

    resp = await client.get("/api/v1/directory?order=new&limit=3&offset=0")
    assert resp.status_code == 200
    assert len(resp.json()) <= 3
