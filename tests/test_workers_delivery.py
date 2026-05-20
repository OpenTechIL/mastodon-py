"""Tests for the delivery fan-out arq job.

Exercises `_run` directly with a real DB session + a fresh httpx
client wrapped in respx, mirroring the worker's pattern. No arq
runtime needed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.python.workers.delivery import _run


def _make_keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, bytes]:
    return _make_keypair()


async def _seed_local_sender(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    *,
    private_key: bytes,
) -> int:
    """Local alice with a real RSA private key set."""
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=1,
                username="alice",
                domain=None,
                uri="https://us.test/users/alice",
                private_key=private_key.decode("utf-8"),
            )
        )
        s.add(seed_data["make_account_stat"](account_id=1))
        await s.commit()
    return 1


@pytest.mark.asyncio
async def test_run_delivers_to_each_inbox(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, _ = keypair
    sender_id = await _seed_local_sender(
        session_factory, seed_data, private_key=priv
    )
    urls = [
        "https://a.test/inbox",
        "https://b.test/inbox",
        "https://c.test/inbox",
    ]
    async with respx.mock() as router:
        for u in urls:
            router.post(u).respond(202)
        async with session_factory() as session:
            async with httpx.AsyncClient() as client:
                report = await _run(
                    session,
                    client,
                    activity={"type": "Follow"},
                    sender_account_id=sender_id,
                    inbox_urls=urls,
                )
    assert report.attempts == 3
    assert report.successes == 3
    assert report.failures == 0


@pytest.mark.asyncio
async def test_run_returns_zero_report_when_sender_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with respx.mock(assert_all_called=False) as router:
        route = router.post("https://a.test/inbox").respond(202)
        async with session_factory() as session:
            async with httpx.AsyncClient() as client:
                report = await _run(
                    session,
                    client,
                    activity={"type": "Follow"},
                    sender_account_id=999_999,
                    inbox_urls=["https://a.test/inbox"],
                )
    assert report.attempts == 0
    # Defensive: we never POSTed since the sender wasn't loadable.
    assert not route.called


@pytest.mark.asyncio
async def test_run_skips_sender_with_no_private_key(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
) -> None:
    """A misconfigured local account (no key yet) shouldn't crash the
    worker — it returns a zero report and the call site can choose
    whether to backfill the key + retry."""
    async with session_factory() as s:
        s.add(
            seed_data["make_account"](
                id_=1, username="alice", domain=None,
                uri="https://us.test/users/alice", private_key="",
            )
        )
        s.add(seed_data["make_account_stat"](account_id=1))
        await s.commit()

    async with respx.mock(assert_all_called=False) as router:
        route = router.post("https://a.test/inbox").respond(202)
        async with session_factory() as session:
            async with httpx.AsyncClient() as client:
                report = await _run(
                    session,
                    client,
                    activity={"type": "Follow"},
                    sender_account_id=1,
                    inbox_urls=["https://a.test/inbox"],
                )
    assert report.attempts == 0
    assert not route.called


@pytest.mark.asyncio
async def test_run_reports_mixed_results(
    session_factory: async_sessionmaker[AsyncSession],
    seed_data: dict[str, Any],
    keypair: tuple[bytes, bytes],
) -> None:
    priv, _ = keypair
    sender_id = await _seed_local_sender(
        session_factory, seed_data, private_key=priv
    )
    async with respx.mock() as router:
        router.post("https://a.test/inbox").respond(202)
        router.post("https://b.test/inbox").respond(500)
        router.post("https://c.test/inbox").mock(
            side_effect=httpx.ConnectError("dns")
        )
        async with session_factory() as session:
            async with httpx.AsyncClient() as client:
                report = await _run(
                    session,
                    client,
                    activity={"type": "Follow"},
                    sender_account_id=sender_id,
                    inbox_urls=[
                        "https://a.test/inbox",
                        "https://b.test/inbox",
                        "https://c.test/inbox",
                    ],
                )
    assert report.attempts == 3
    assert report.successes == 1
    assert report.failures == 2


def test_worker_settings_registers_deliver_activity() -> None:
    """arq's CLI dispatch table must include the new job."""
    from app.python.workers.arq_settings import WorkerSettings
    from app.python.workers.delivery import deliver_activity

    assert deliver_activity in WorkerSettings.functions
