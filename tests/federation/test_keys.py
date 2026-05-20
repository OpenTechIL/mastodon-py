"""Tests for `ensure_local_actor_keys`."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    RSAPublicKey,
)

from app.python.federation.keys import ensure_local_actor_keys
from app.python.federation.signatures import sign_request, verify_request
from app.python.models import Account


def _make_local_account(**overrides) -> Account:
    from datetime import datetime, timezone

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    defaults = {
        "id": 1,
        "username": "alice",
        "domain": None,
        "display_name": "Alice",
        "note": "",
        "uri": "https://us.test/users/alice",
        "url": None,
        "header_remote_url": "",
        "public_key": "",
        "private_key": "",
        "inbox_url": "",
        "shared_inbox_url": "",
        "locked": False,
        "indexable": False,
        "memorial": False,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)
    return Account(**defaults)  # type: ignore[arg-type]


def test_ensure_keys_fills_in_pem_pair_for_local_account() -> None:
    account = _make_local_account()
    ensure_local_actor_keys(account)
    assert account.private_key.startswith("-----BEGIN PRIVATE KEY-----")
    assert account.public_key.startswith("-----BEGIN PUBLIC KEY-----")
    # The PEM strings load as RSA keys cryptography can sign with.
    priv = serialization.load_pem_private_key(
        account.private_key.encode(), password=None
    )
    pub = serialization.load_pem_public_key(account.public_key.encode())
    assert isinstance(priv, RSAPrivateKey)
    assert isinstance(pub, RSAPublicKey)


def test_ensure_keys_is_noop_on_remote_account() -> None:
    """Remote actors carry keys from their home server; we never
    overwrite or fill them in."""
    account = _make_local_account(domain="other.test", private_key="", public_key="")
    ensure_local_actor_keys(account)
    assert account.private_key == ""
    assert account.public_key == ""


def test_ensure_keys_is_idempotent_when_already_set() -> None:
    """Calling twice must NOT regenerate — that would invalidate every
    signature we've ever made."""
    account = _make_local_account()
    ensure_local_actor_keys(account)
    first_priv = account.private_key
    first_pub = account.public_key
    ensure_local_actor_keys(account)
    assert account.private_key == first_priv
    assert account.public_key == first_pub


def test_generated_keypair_can_sign_and_verify_a_request() -> None:
    """End-to-end: generate keys, sign a request with the private half,
    verify with the public half. Catches any encoding mismatch
    between the PEM strings we persist and what the signers expect."""
    account = _make_local_account()
    ensure_local_actor_keys(account)

    headers: dict[str, str] = {
        "Host": "other.test",
        "Content-Type": "application/activity+json",
    }
    body = b'{"type":"Follow"}'
    sign_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=body,
        key_id=f"{account.uri}#main-key",
        private_key_pem=account.private_key.encode("utf-8"),
    )
    assert verify_request(
        method="POST",
        path="/inbox",
        headers=headers,
        body=body,
        public_key_pem=account.public_key.encode("utf-8"),
    )
