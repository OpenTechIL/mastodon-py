"""Tests for `sign_and_deliver`.

Beyond happy-path success, the round-trip test signs as our local
actor and verifies the POST with our own `verify_request` — proving
the wire format is symmetric with the inbound side.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.python.federation.delivery import sign_and_deliver
from app.python.federation.signatures import verify_request


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


class _Sender:
    """Minimal Account stand-in. Avoids dragging in the seed factories
    when all the delivery path reads is `.uri` and `.private_key`."""

    def __init__(self, uri: str, private_key: bytes) -> None:
        self.uri = uri
        self.private_key = private_key.decode("utf-8")


@pytest.mark.asyncio
async def test_delivery_signs_and_posts_body(
    keypair: tuple[bytes, bytes],
) -> None:
    priv, pub = keypair
    sender = _Sender("https://us.test/users/alice", priv)
    activity = {"type": "Follow", "actor": sender.uri, "object": "https://other.test/users/bob"}

    async with respx.mock() as router:
        route = router.post("https://other.test/users/bob/inbox").respond(202)
        async with httpx.AsyncClient() as client:
            ok = await sign_and_deliver(
                activity=activity,
                sender=sender,  # type: ignore[arg-type]
                recipient_inbox_url="https://other.test/users/bob/inbox",
                http_client=client,
            )
    assert ok is True
    assert route.called
    request = route.calls.last.request
    # Body is the serialized activity.
    assert request.content
    body = request.content
    # Signature + Digest headers are present.
    assert "Signature" in request.headers
    assert "Digest" in request.headers
    # `keyId` references the sender's main-key URI.
    assert f'keyId="{sender.uri}#main-key"' in request.headers["Signature"]


@pytest.mark.asyncio
async def test_delivery_signature_verifies_against_sender_pubkey(
    keypair: tuple[bytes, bytes],
) -> None:
    """Round-trip: deliver an activity, intercept the POST, run the
    inbound verifier against the recorded headers + body. This catches
    any mismatch between sign-side and verify-side canonical strings."""
    priv, pub = keypair
    sender = _Sender("https://us.test/users/alice", priv)
    activity = {"type": "Create", "actor": sender.uri}

    async with respx.mock() as router:
        route = router.post("https://other.test/inbox").respond(202)
        async with httpx.AsyncClient() as client:
            await sign_and_deliver(
                activity=activity,
                sender=sender,  # type: ignore[arg-type]
                recipient_inbox_url="https://other.test/inbox",
                http_client=client,
            )
    request = route.calls.last.request
    assert verify_request(
        method="POST",
        path=request.url.raw_path.decode(),
        headers=dict(request.headers),
        body=request.content,
        public_key_pem=pub,
    )


@pytest.mark.asyncio
async def test_delivery_returns_false_on_4xx(
    keypair: tuple[bytes, bytes],
) -> None:
    priv, _pub = keypair
    sender = _Sender("https://us.test/users/alice", priv)
    async with respx.mock() as router:
        router.post("https://other.test/inbox").respond(401)
        async with httpx.AsyncClient() as client:
            ok = await sign_and_deliver(
                activity={"type": "Create"},
                sender=sender,  # type: ignore[arg-type]
                recipient_inbox_url="https://other.test/inbox",
                http_client=client,
            )
    assert ok is False


@pytest.mark.asyncio
async def test_delivery_returns_false_on_5xx(
    keypair: tuple[bytes, bytes],
) -> None:
    priv, _pub = keypair
    sender = _Sender("https://us.test/users/alice", priv)
    async with respx.mock() as router:
        router.post("https://other.test/inbox").respond(503)
        async with httpx.AsyncClient() as client:
            ok = await sign_and_deliver(
                activity={"type": "Create"},
                sender=sender,  # type: ignore[arg-type]
                recipient_inbox_url="https://other.test/inbox",
                http_client=client,
            )
    assert ok is False


@pytest.mark.asyncio
async def test_delivery_returns_false_on_network_error(
    keypair: tuple[bytes, bytes],
) -> None:
    """Connection errors must not surface to the worker — they're
    routine, transient, and only meaningful in aggregate."""
    priv, _pub = keypair
    sender = _Sender("https://us.test/users/alice", priv)
    async with respx.mock() as router:
        router.post("https://other.test/inbox").mock(
            side_effect=httpx.ConnectError("dns")
        )
        async with httpx.AsyncClient() as client:
            ok = await sign_and_deliver(
                activity={"type": "Create"},
                sender=sender,  # type: ignore[arg-type]
                recipient_inbox_url="https://other.test/inbox",
                http_client=client,
            )
    assert ok is False


@pytest.mark.asyncio
async def test_delivery_skips_when_sender_has_no_private_key() -> None:
    """A remote actor (no private key) accidentally passed as sender
    must not crash — return False, no fetch."""
    sender = _Sender("https://us.test/users/alice", b"")
    async with respx.mock(assert_all_called=False) as router:
        route = router.post("https://other.test/inbox")
        async with httpx.AsyncClient() as client:
            ok = await sign_and_deliver(
                activity={"type": "Create"},
                sender=sender,  # type: ignore[arg-type]
                recipient_inbox_url="https://other.test/inbox",
                http_client=client,
            )
    assert ok is False
    assert not route.called


@pytest.mark.asyncio
async def test_delivery_rejects_non_http_inbox_url(
    keypair: tuple[bytes, bytes],
) -> None:
    priv, _pub = keypair
    sender = _Sender("https://us.test/users/alice", priv)
    async with httpx.AsyncClient() as client:
        ok = await sign_and_deliver(
            activity={"type": "Create"},
            sender=sender,  # type: ignore[arg-type]
            recipient_inbox_url="not-a-url",
            http_client=client,
        )
    assert ok is False
