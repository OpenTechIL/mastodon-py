"""/api/web/* endpoints used by the React SPA.

web/settings — persists per-user UI preferences (column layout, theme, etc.)
web/push_subscriptions — stub; web push requires VAPID keys not yet configured
web/embeds/:id — oEmbed-style HTML snippet for status embedding
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from app.python.deps import CurrentAccount, DBSession, OptionalAuth
from app.python.lib.asset_urls import avatar_url
from app.python.models import Account, Status, User
from app.python.models.web_setting import WebSetting
from app.python.settings import get_settings

router = APIRouter(prefix="/api/web", tags=["web-api"])


class _WebSettingsBody(BaseModel):
    data: dict[str, Any] | None = None


@router.get("/settings")
async def get_web_settings(
    account: CurrentAccount,
    session: DBSession,
) -> dict:
    """Return the saved web settings for the current user."""
    user = (
        await session.execute(select(User).where(User.account_id == account.id))
    ).scalar_one_or_none()
    if user is None:
        return {}
    row = (
        await session.execute(select(WebSetting).where(WebSetting.user_id == user.id))
    ).scalar_one_or_none()
    return dict(row.data) if row and row.data else {}


@router.put("/settings", status_code=status.HTTP_204_NO_CONTENT)
async def update_web_settings(
    account: CurrentAccount,
    session: DBSession,
    body: _WebSettingsBody | None = None,
) -> Response:
    user = (
        await session.execute(select(User).where(User.account_id == account.id))
    ).scalar_one_or_none()
    if user is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    row = (
        await session.execute(select(WebSetting).where(WebSetting.user_id == user.id))
    ).scalar_one_or_none()
    data = body.data if body else {}
    now = datetime.now(UTC).replace(tzinfo=None)
    if row is None:
        row = WebSetting(user_id=user.id, data=data or {}, created_at=now, updated_at=now)
        session.add(row)
    else:
        row.data = data or {}
        row.updated_at = now
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Push subscriptions stub ───────────────────────────────────────────────────

@router.post("/push_subscriptions")
async def create_push_subscription() -> dict[str, Any]:
    """Stub: returns an empty object; VAPID push not yet configured."""
    return {}


@router.put("/push_subscriptions/{subscription_id}")
async def update_push_subscription(subscription_id: int) -> dict[str, Any]:
    return {}


@router.delete("/push_subscriptions/{subscription_id}", status_code=status.HTTP_200_OK)
async def delete_push_subscription(subscription_id: int) -> Response:
    return Response(status_code=status.HTTP_200_OK)


# ── Embeds (oEmbed-style HTML snippet) ───────────────────────────────────────

@router.get("/embeds/{status_id}")
async def get_embed(
    status_id: int,
    session: DBSession,
    auth: OptionalAuth,
) -> dict[str, Any]:
    """Return an inline-styled HTML snippet for embedding a status."""
    row = (
        await session.execute(
            select(Status).where(Status.id == status_id, Status.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    from app.python.models.status import Visibility
    if row is None or row.visibility not in {Visibility.PUBLIC.value, Visibility.UNLISTED.value}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    s = get_settings()
    base = s.base_url()
    account: Account = row.account
    status_url = f"{base}/@{account.acct}/{row.id}"
    acct_avatar = avatar_url(account)

    html = (
        f'<blockquote class="mastodon-embed" data-embed-url="{status_url}/embed" '
        f'style="background:#FCF8FF;border-radius:8px;border:1px solid #C9C4DA;'
        f'margin:0;max-width:540px;min-width:270px;overflow:hidden;padding:0;">'
        f'<a href="{status_url}" target="_blank" style="align-items:center;color:#1C1A25;'
        f'display:flex;flex-direction:column;font-family:system-ui,-apple-system,sans-serif;'
        f'font-size:14px;justify-content:center;letter-spacing:0.25px;line-height:20px;'
        f'padding:24px;text-decoration:none;">'
        f'<div style="color:#787588;margin-top:16px;">Post by @{account.acct}</div>'
        f'<div style="font-weight:500;">View on {s.effective_web_domain}</div>'
        f'</a></blockquote>'
        f'<script data-allowed-prefixes="{base}/" async src="{base}/embed.js"></script>'
    )
    return {"type": "rich", "version": "1.0", "html": html, "author_name": account.display_name or account.username,
            "author_url": f"{base}/@{account.acct}", "provider_name": s.effective_web_domain,
            "provider_url": base, "thumbnail_url": acct_avatar}
