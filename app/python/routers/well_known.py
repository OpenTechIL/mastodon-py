"""`.well-known` endpoints — currently just WebFinger.

WebFinger is the discovery handshake every federated peer performs
before fetching one of our actors. Given an `acct:user@host` query, we
return a JRD pointing at the AP actor JSON and HTML profile page. The
remote then follows the `self`/`application/activity+json` link to
load the full Person object.

Mastodon's WebFinger contract:

  GET /.well-known/webfinger?resource=acct:alice@example.test

  → 200 with `application/jrd+json`:
    {
      "subject": "acct:alice@example.test",
      "aliases": [...],
      "links": [{rel, type, href}, ...]
    }

  → 404 if the username doesn't exist locally or the host portion
    doesn't match this server.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from app.python.deps import DBSession
from app.python.lib.asset_urls import _asset_host  # noqa: PLC2701
from app.python.models import Account
from app.python.settings import get_settings

router = APIRouter(tags=["well-known"])


def _parse_acct(resource: str) -> tuple[str, str] | None:
    """Pull `(username, host)` out of `acct:user@host`. Also accepts
    bare `user@host` (some clients drop the `acct:` prefix)."""
    raw = resource.removeprefix("acct:").lstrip("@")
    if "@" not in raw:
        return None
    username, _, host = raw.partition("@")
    if not username or not host:
        return None
    return username, host


@router.get("/.well-known/webfinger")
async def webfinger(
    session: DBSession,
    resource: str = Query(...),
) -> Response:
    parsed = _parse_acct(resource)
    if parsed is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Invalid resource"
        )
    username, host = parsed

    settings = get_settings()
    expected_host = settings.local_domain
    expected_web = settings.effective_web_domain
    if host not in {expected_host, expected_web}:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Record not found"
        )

    row = (
        await session.execute(
            select(Account).where(
                Account.username == username, Account.domain.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Record not found"
        )

    asset_host = _asset_host()
    actor_url = f"{asset_host}/users/{row.username}"
    profile_url = f"{asset_host}/@{row.username}"
    body = {
        "subject": f"acct:{row.username}@{expected_host}",
        "aliases": [profile_url, actor_url],
        "links": [
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": profile_url,
            },
            {
                "rel": "self",
                "type": "application/activity+json",
                "href": actor_url,
            },
        ],
    }
    import json

    # Mastodon and the wider Fediverse expect `application/jrd+json`
    # specifically — some peers reject `application/json` here.
    return Response(
        content=json.dumps(body),
        media_type="application/jrd+json",
    )
