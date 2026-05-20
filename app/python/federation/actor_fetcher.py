"""Fetch a remote AP actor and persist an Account stub.

When a signed activity arrives from an actor we've never seen, we need
their Account row so the handler has something to attach
follows/likes/boosts to. This is the first-contact path:

  1. GET the actor URL with `Accept: application/activity+json`.
  2. Parse the JSON, extract `preferredUsername`, `inbox`,
     `publicKey.publicKeyPem`, etc.
  3. Derive `(username, domain)` — username from `preferredUsername`,
     domain from the URL's host.
  4. INSERT a new Account with `domain != NULL`.

Returns the persisted Account on success, None on any failure (bad URL,
HTTP error, malformed JSON, missing required fields). On race with a
concurrent fetch we read-after-insert.

This slice does NOT:
  - Fetch the actor's avatar/header (file transfer is its own thing).
  - Backfill follower/following counts.
  - Refresh stale rows (we always return the existing row if present).

Mastodon's `ActivityPub::FetchRemoteAccountService` does all three; we
port them when the inbound flows demand it.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from sqlalchemy import select

from app.python.common.snowflake import now_id
from app.python.models import Account

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession


_ACTOR_ACCEPT = (
    "application/activity+json,"
    'application/ld+json;profile="https://www.w3.org/ns/activitystreams"'
)


async def fetch_and_persist_actor(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    actor_url: str,
) -> Account | None:
    """Return the local Account row for `actor_url`, fetching it from
    the remote server if we don't have it yet."""
    if not actor_url:
        return None

    existing = (
        await session.execute(select(Account).where(Account.uri == actor_url))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    parsed = urlparse(actor_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    try:
        response = await http_client.get(
            actor_url, headers={"Accept": _ACTOR_ACCEPT}
        )
    except Exception:
        return None
    if response.status_code != 200:
        return None
    try:
        actor = response.json()
    except _json.JSONDecodeError:
        return None
    if not isinstance(actor, dict):
        return None

    username = actor.get("preferredUsername")
    if not isinstance(username, str) or not username:
        return None

    pem = _extract_pem(actor)
    inbox = actor.get("inbox")
    shared_inbox = _shared_inbox(actor)
    display_name = actor.get("name") if isinstance(actor.get("name"), str) else ""
    summary = actor.get("summary") if isinstance(actor.get("summary"), str) else ""
    profile_url = actor.get("url") if isinstance(actor.get("url"), str) else None
    actor_type = actor.get("type") if isinstance(actor.get("type"), str) else "Person"
    locked = bool(actor.get("manuallyApprovesFollowers", False))
    discoverable = actor.get("discoverable")
    indexable = bool(actor.get("indexable", False))

    # Re-check under "real" SQL — another worker may have inserted
    # this actor between our select and now.
    existing = (
        await session.execute(select(Account).where(Account.uri == actor_url))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    row = Account(
        id=now_id(),
        username=username,
        domain=parsed.netloc,
        display_name=display_name,
        note=summary,
        actor_type=actor_type,
        locked=locked,
        discoverable=discoverable,
        indexable=indexable,
        memorial=False,
        hide_collections=None,
        uri=actor_url,
        url=profile_url,
        public_key=pem or "",
        inbox_url=inbox if isinstance(inbox, str) else "",
        shared_inbox_url=shared_inbox,
        avatar_remote_url=None,
        avatar_content_type=None,
        header_remote_url="",
        header_content_type=None,
        suspended_at=None,
        silenced_at=None,
        suspension_origin=None,
        sensitized_at=None,
        fields=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return row


def _extract_pem(actor: dict[str, Any]) -> str | None:
    pk = actor.get("publicKey")
    if isinstance(pk, list):
        pk = next((p for p in pk if isinstance(p, dict)), None)
    if not isinstance(pk, dict):
        return None
    pem = pk.get("publicKeyPem")
    if isinstance(pem, str) and "BEGIN PUBLIC KEY" in pem:
        return pem
    return None


def _shared_inbox(actor: dict[str, Any]) -> str:
    """`endpoints.sharedInbox` if advertised, else empty string."""
    endpoints = actor.get("endpoints")
    if isinstance(endpoints, dict):
        si = endpoints.get("sharedInbox")
        if isinstance(si, str):
            return si
    return ""
