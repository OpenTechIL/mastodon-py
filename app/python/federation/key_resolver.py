"""Resolve a `keyId` URL to its actor public key.

`keyId` in an inbound signature is a URL pointing at the signer's
public key, typically something like:

  https://mastodon.example/users/alice#main-key

Strip the fragment, look the actor up locally first (we may have
ingested them before), and fall back to an HTTP GET of the actor JSON
when we haven't. Either way, return the PEM-encoded RSA public key
the signature was made with, or None if we couldn't find one.

This is a thin lookup — persistence + counter-cache updates belong in
a higher-level "fetch and remember actor" service that ports later
during Phase 6.
"""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING
from urllib.parse import urldefrag

from sqlalchemy import select

from app.python.models import Account

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession


# Mastodon and most AP servers serve actors with this Accept header.
# Some return `application/ld+json` instead — both decode the same.
_ACTOR_ACCEPT = (
    "application/activity+json,"
    'application/ld+json;profile="https://www.w3.org/ns/activitystreams"'
)


async def resolve_public_key(
    *,
    key_id: str,
    session: AsyncSession,
    http_client: httpx.AsyncClient,
) -> bytes | None:
    """Return the PEM public key for `key_id`, or None.

    Local-first: if an `accounts` row has its URI matching the
    fragment-stripped keyId AND has a populated `public_key`, use that.
    Otherwise GET the actor JSON, dig out `publicKey.publicKeyPem`.
    """
    actor_url, _frag = urldefrag(key_id)
    pem = await _local_lookup(session, actor_url=actor_url)
    if pem is not None:
        return pem
    return await _http_lookup(http_client, key_id=key_id, actor_url=actor_url)


async def _local_lookup(
    session: AsyncSession, *, actor_url: str
) -> bytes | None:
    if not actor_url:
        return None
    row = (
        await session.execute(
            select(Account).where(Account.uri == actor_url)
        )
    ).scalar_one_or_none()
    if row is None or not row.public_key:
        return None
    return row.public_key.encode("utf-8")


async def _http_lookup(
    client: httpx.AsyncClient, *, key_id: str, actor_url: str
) -> bytes | None:
    if not actor_url.startswith(("https://", "http://")):
        return None
    try:
        response = await client.get(actor_url, headers={"Accept": _ACTOR_ACCEPT})
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        actor = response.json()
    except _json.JSONDecodeError:
        return None
    return _extract_pem(actor, want_key_id=key_id)


def _extract_pem(actor: dict, *, want_key_id: str) -> bytes | None:
    """Pull `publicKey.publicKeyPem` out of an actor JSON document.

    `publicKey` may be a single object or a list of them. Match by
    `id == want_key_id` when there's a choice — actors may rotate
    keys and advertise both during the overlap.
    """
    if not isinstance(actor, dict):
        return None
    public_key = actor.get("publicKey")
    if public_key is None:
        return None
    candidates = public_key if isinstance(public_key, list) else [public_key]
    chosen: dict | None = None
    for c in candidates:
        if not isinstance(c, dict):
            continue
        if c.get("id") == want_key_id:
            chosen = c
            break
        if chosen is None:
            chosen = c
    if chosen is None:
        return None
    pem = chosen.get("publicKeyPem")
    if not isinstance(pem, str) or "BEGIN PUBLIC KEY" not in pem:
        return None
    return pem.encode("utf-8")
