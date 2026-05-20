"""Mute / unmute.

Soft cut. The follow graph is untouched; only the muter's view of the
target's content changes. Optional duration (seconds) sets `expires_at`;
`notifications=true` (default) also suppresses notifications from the
target.

Mute-expiry sweeping (a periodic job that deletes rows past
`expires_at`) lands with the scheduler/cron phase. Until then expired
mutes remain on the row but the filters here check `expires_at` on
every read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import now_id
from app.python.models import Account, Mute


class SelfMute(Exception):
    """Raised when an account tries to mute itself."""


async def mute(
    session: AsyncSession,
    *,
    source: Account,
    target: Account,
    hide_notifications: bool = True,
    duration_seconds: int | None = None,
) -> Mute:
    if source.id == target.id:
        raise SelfMute

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    expires_at = (
        now + timedelta(seconds=duration_seconds) if duration_seconds else None
    )

    existing = (
        await session.execute(
            select(Mute).where(
                Mute.account_id == source.id,
                Mute.target_account_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Mastodon treats a repeat mute call as "update settings."
        existing.hide_notifications = hide_notifications
        existing.expires_at = expires_at
        existing.updated_at = now
        await session.commit()
        return existing

    row = Mute(
        id=now_id(),
        account_id=source.id,
        target_account_id=target.id,
        hide_notifications=hide_notifications,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def unmute(
    session: AsyncSession,
    *,
    source: Account,
    target: Account,
) -> bool:
    result = await session.execute(
        delete(Mute).where(
            Mute.account_id == source.id,
            Mute.target_account_id == target.id,
        )
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        return False
    await session.commit()
    return True
