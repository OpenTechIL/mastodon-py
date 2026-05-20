"""Local-actor RSA keypair management.

Every local actor needs an RSA keypair so remote peers can verify our
outbound HTTP signatures. Mastodon generates these on first save; we
replicate that behavior with `ensure_local_actor_keys`, which fills in
`public_key` + `private_key` for any local account missing them.

The function is a no-op on:
  - Remote rows (`domain` set) — their keys come from their server.
  - Local rows that already have a private key — never regenerate; doing
    so would invalidate every outbound signature we've ever made.

Mutates `account` in place. Caller commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

if TYPE_CHECKING:
    from app.python.models import Account


# 2048 bits is what Mastodon emits today. RFC 8017 recommends 2048+;
# 4096 is overkill for AP and slows verification on every receiving
# peer.
_RSA_KEY_SIZE = 2048


def ensure_local_actor_keys(account: Account) -> None:
    """Generate + persist a keypair if `account` is local and lacks one."""
    if account.domain is not None:
        return  # remote actor — we don't own its keys
    if account.private_key:
        return  # already have a keypair; never regenerate

    key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_KEY_SIZE)
    account.private_key = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    account.public_key = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
