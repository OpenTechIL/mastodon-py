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

import json
import os

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select

from app.python.deps import DBSession
from app.python.lib.asset_urls import _asset_host
from app.python.models import Account, Status, User
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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid resource")
    username, host = parsed

    settings = get_settings()
    expected_host = settings.local_domain
    expected_web = settings.effective_web_domain
    if host not in {expected_host, expected_web}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    row = (
        await session.execute(select(Account).where(Account.username == username, Account.domain.is_(None)))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

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
    # Mastodon and the wider Fediverse expect `application/jrd+json`
    # specifically — some peers reject `application/json` here.
    return Response(
        content=json.dumps(body),
        media_type="application/jrd+json",
    )


# ── NodeInfo ──────────────────────────────────────────────────────────────────


@router.get("/.well-known/nodeinfo")
async def nodeinfo_discovery() -> Response:
    """NodeInfo discovery document — points to the 2.0 schema endpoint."""
    settings = get_settings()
    host = _asset_host()
    body = {
        "links": [
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.0",
                "href": f"{host}/nodeinfo/2.0",
            }
        ]
    }
    return Response(
        content=json.dumps(body),
        media_type='application/json; profile="http://nodeinfo.diaspora.software/ns/schema/2.0#"',
    )


@router.get("/nodeinfo/2.0")
async def nodeinfo_schema(session: DBSession) -> Response:
    """NodeInfo 2.0 — server software, usage stats, and capabilities."""
    settings = get_settings()
    user_count = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    status_count = (
        await session.execute(
            select(func.count()).select_from(Status).where(Status.deleted_at.is_(None), Status.local.is_(True))
        )
    ).scalar_one()
    body = {
        "version": "2.0",
        "software": {"name": "mastodon", "version": "4.3.0+python"},
        "protocols": ["activitypub"],
        "services": {"outbound": [], "inbound": []},
        "usage": {
            "users": {
                "total": int(user_count or 0),
                "activeMonth": int(user_count or 0),
                "activeHalfyear": int(user_count or 0),
            },
            "localPosts": int(status_count or 0),
        },
        "openRegistrations": True,
        "metadata": {
            "nodeName": settings.local_domain,
            "nodeDescription": "",
        },
    }
    return Response(
        content=json.dumps(body),
        media_type='application/json; profile="http://nodeinfo.diaspora.software/ns/schema/2.0#"',
    )


# ── Web App Manifest ──────────────────────────────────────────────────────────

_ANDROID_ICON_SIZES = [36, 48, 72, 96, 144, 192, 256, 384, 512]


@router.get("/manifest.json")
async def web_manifest() -> Response:
    """PWA manifest — enables Add to Home Screen on mobile browsers."""
    settings = get_settings()
    host = _asset_host()
    domain = settings.local_domain

    icons = []
    for size in _ANDROID_ICON_SIZES:
        src = (
            f"{host}/icon-{size}.png"
            if os.path.isfile(f"public/icon-{size}.png")
            else f"{host}/packs-dev/icons/android-chrome-{size}x{size}.png"
        )
        icons.append(
            {
                "src": src,
                "sizes": f"{size}x{size}",
                "type": "image/png",
                "purpose": "any maskable",
            }
        )

    body = {
        "id": "/home",
        "name": domain,
        "short_name": domain,
        "icons": icons,
        "theme_color": "#191b22",
        "background_color": "#191b22",
        "display": "standalone",
        "start_url": "/",
        "scope": "/",
        "share_target": {
            "action": "/share",
            "method": "GET",
            "params": {"title": "title", "text": "text", "url": "url"},
        },
        "shortcuts": [
            {"name": "Compose", "url": "/web/statuses/new", "icons": []},
            {"name": "Explore", "url": "/web/explore", "icons": []},
            {"name": "Notifications", "url": "/web/notifications", "icons": []},
        ],
        "prefer_related_applications": False,
        "related_applications": [],
    }
    return Response(
        content=json.dumps(body),
        media_type="application/manifest+json",
    )


# ── oauth-authorization-server ────────────────────────────────────────────────


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server() -> Response:
    """OAuth 2.0 Authorization Server Metadata (RFC 8414)."""
    settings = get_settings()
    host = _asset_host()
    body = {
        "issuer": host,
        "authorization_endpoint": f"{host}/oauth/authorize",
        "token_endpoint": f"{host}/oauth/token",
        "revocation_endpoint": f"{host}/oauth/revoke",
        "scopes_supported": ["read", "write", "follow", "push", "admin:read", "admin:write"],
        "response_types_supported": ["code"],
        "response_modes_supported": ["query", "fragment"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
    }
    return Response(
        content=json.dumps(body),
        media_type="application/json",
    )
