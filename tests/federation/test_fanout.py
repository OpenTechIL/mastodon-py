"""Tests for the outbound fan-out helper."""

from __future__ import annotations

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.python.federation.fanout import (
    DeliveryReport,
    collect_inbox_urls,
    deliver_to_inboxes,
)


# ---------- collect_inbox_urls ----------


class _Acct:
    """Minimal stand-in for Account. The fan-out helper only reads
    `inbox_url` and `shared_inbox_url`, so the seed factories aren't
    worth the indirection here."""

    def __init__(self, inbox: str = "", shared: str = "") -> None:
        self.inbox_url = inbox
        self.shared_inbox_url = shared


def test_collect_prefers_shared_inbox_when_set() -> None:
    accounts = [
        _Acct(inbox="https://a.test/users/u1/inbox", shared="https://a.test/inbox"),
        _Acct(inbox="https://a.test/users/u2/inbox", shared="https://a.test/inbox"),
    ]
    # Two followers on the same server collapse to one POST.
    assert collect_inbox_urls(accounts) == ["https://a.test/inbox"]


def test_collect_falls_back_to_inbox_when_no_shared() -> None:
    accounts = [
        _Acct(inbox="https://a.test/users/u/inbox"),
        _Acct(inbox="https://b.test/users/u/inbox"),
    ]
    assert collect_inbox_urls(accounts) == [
        "https://a.test/users/u/inbox",
        "https://b.test/users/u/inbox",
    ]


def test_collect_mixed_shared_and_per_actor() -> None:
    accounts = [
        _Acct(inbox="https://a.test/users/u1/inbox", shared="https://a.test/inbox"),
        _Acct(inbox="https://b.test/users/u/inbox"),  # no shared inbox
        _Acct(inbox="https://a.test/users/u2/inbox", shared="https://a.test/inbox"),
    ]
    out = collect_inbox_urls(accounts)
    assert set(out) == {"https://a.test/inbox", "https://b.test/users/u/inbox"}
    # Order preserved by first appearance.
    assert out[0] == "https://a.test/inbox"


def test_collect_skips_accounts_with_no_inbox() -> None:
    accounts = [_Acct(inbox="", shared=""), _Acct(inbox="https://a.test/inbox")]
    assert collect_inbox_urls(accounts) == ["https://a.test/inbox"]


# ---------- deliver_to_inboxes ----------


def _make_sender() -> _Acct:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    class _Sender:
        uri = "https://us.test/users/alice"
        private_key = priv

    return _Sender()  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_delivery_to_empty_list_is_zero_report() -> None:
    sender = _make_sender()
    async with httpx.AsyncClient() as client:
        report = await deliver_to_inboxes(
            activity={"type": "Create"},
            sender=sender,  # type: ignore[arg-type]
            inbox_urls=[],
            http_client=client,
        )
    assert report == DeliveryReport(attempts=0, successes=0, failures=0)


@pytest.mark.asyncio
async def test_delivery_reports_all_success() -> None:
    sender = _make_sender()
    urls = [
        "https://a.test/inbox",
        "https://b.test/inbox",
        "https://c.test/inbox",
    ]
    async with respx.mock() as router:
        for u in urls:
            router.post(u).respond(202)
        async with httpx.AsyncClient() as client:
            report = await deliver_to_inboxes(
                activity={"type": "Follow"},
                sender=sender,  # type: ignore[arg-type]
                inbox_urls=urls,
                http_client=client,
            )
    assert report == DeliveryReport(attempts=3, successes=3, failures=0)


@pytest.mark.asyncio
async def test_delivery_reports_mixed_success_and_failure() -> None:
    sender = _make_sender()
    async with respx.mock() as router:
        router.post("https://a.test/inbox").respond(202)
        router.post("https://b.test/inbox").respond(500)
        router.post("https://c.test/inbox").mock(
            side_effect=httpx.ConnectError("dns")
        )
        async with httpx.AsyncClient() as client:
            report = await deliver_to_inboxes(
                activity={"type": "Follow"},
                sender=sender,  # type: ignore[arg-type]
                inbox_urls=[
                    "https://a.test/inbox",
                    "https://b.test/inbox",
                    "https://c.test/inbox",
                ],
                http_client=client,
            )
    assert report == DeliveryReport(attempts=3, successes=1, failures=2)


@pytest.mark.asyncio
async def test_delivery_posts_to_every_inbox() -> None:
    """All recipients receive their POST. Verified via respx call
    counts rather than timing — respx's router doesn't truly process
    async side effects in parallel, so timing-based concurrency
    assertions are flaky."""
    sender = _make_sender()
    urls = [f"https://r{i}.test/inbox" for i in range(5)]
    async with respx.mock() as router:
        routes = [router.post(u).respond(202) for u in urls]
        async with httpx.AsyncClient() as client:
            report = await deliver_to_inboxes(
                activity={"type": "Create"},
                sender=sender,  # type: ignore[arg-type]
                inbox_urls=urls,
                http_client=client,
            )
    assert report.successes == 5
    assert all(r.called for r in routes)


@pytest.mark.asyncio
async def test_max_concurrency_does_not_drop_requests() -> None:
    """Tight semaphore (=1) still completes all recipients, just serially."""
    sender = _make_sender()
    urls = [f"https://r{i}.test/inbox" for i in range(3)]
    async with respx.mock() as router:
        routes = [router.post(u).respond(202) for u in urls]
        async with httpx.AsyncClient() as client:
            report = await deliver_to_inboxes(
                activity={"type": "Create"},
                sender=sender,  # type: ignore[arg-type]
                inbox_urls=urls,
                http_client=client,
                max_concurrency=1,
            )
    assert report.successes == 3
    assert all(r.called for r in routes)
