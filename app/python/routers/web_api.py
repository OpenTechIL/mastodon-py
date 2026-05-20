"""/api/web/* endpoints used by the React SPA.

web/settings — persists per-user UI preferences (column layout, theme, etc.)
web/push_subscriptions — stub; web push requires VAPID keys not yet configured
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from app.python.deps import CurrentAccount, DBSession
from app.python.models import User
from app.python.models.web_setting import WebSetting

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
    return row.data if row else {}


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
    now = datetime.now(timezone.utc).replace(tzinfo=None)
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
