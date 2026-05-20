"""Local notification creation.

A "local" notification is one that lands in the recipient's
notifications column. We only create one when the recipient is a local
user — remote recipients learn of the action via ActivityPub `Like` /
`Announce` / `Follow` delivery, which lands in the federation phase.

Self-actions don't notify: if alice favourites her own post, no row.

Deferred to dedicated phases:

  - WebPush delivery (`webpush_subscriptions` table not modeled).
  - Notification-grouping (`group_key` column) for the post-4.3
    UI consolidation. Phase 0 leaves the column nullable; this slice
    writes `None`, matching the legacy "ungrouped" behavior.
  - Per-account notification filters (`notification_policies` /
    `notification_requests` tables not modeled).
  - Mention / poll / update / admin notification types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import now_id
from app.python.models import (
    ACTIVITY_TYPE_FOR,
    Account,
    Notification,
    NotificationType,
    User,
)


async def _is_local(session: AsyncSession, account_id: int) -> bool:
    """An account is local when it has a `users` row."""
    row = (
        await session.execute(
            select(User.id).where(User.account_id == account_id).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def create_local(
    session: AsyncSession,
    *,
    recipient: Account,
    actor: Account,
    activity_id: int,
    type: NotificationType,
) -> Notification | None:
    """Insert a Notification row and return it, or None if the recipient
    is remote / self-acting.

    Caller must commit (or be inside a session that will commit). We
    deliberately *don't* commit here so the notification creation is
    transactionally bundled with the underlying side-effect (e.g. the
    Favourite row creation).
    """
    if recipient.id == actor.id:
        return None
    if not await _is_local(session, recipient.id):
        return None

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    row = Notification(
        id=now_id(),
        account_id=recipient.id,
        from_account_id=actor.id,
        activity_id=activity_id,
        activity_type=ACTIVITY_TYPE_FOR[type],
        type=type.value,
        filtered=False,
        group_key=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    return row
