"""Bookmark / unbookmark.

Bookmarks have no counter cache and no notification (they're invisible
to the bookmarked account), so the service is essentially a vanilla
upsert/delete with a visibility check.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import now_id
from app.python.models import Account, Bookmark, Status
from app.python.policies.status_policy import visible_to
from app.python.services.favourites import StatusNotFound


async def bookmark(
    session: AsyncSession,
    *,
    account: Account,
    status: Status,
) -> Bookmark:
    if not await visible_to(session, status, account.id):
        raise StatusNotFound

    existing = (
        await session.execute(
            select(Bookmark).where(
                Bookmark.account_id == account.id,
                Bookmark.status_id == status.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    row = Bookmark(
        id=now_id(),
        account_id=account.id,
        status_id=status.id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def unbookmark(
    session: AsyncSession,
    *,
    account: Account,
    status: Status,
) -> bool:
    result = await session.execute(
        delete(Bookmark).where(
            Bookmark.account_id == account.id,
            Bookmark.status_id == status.id,
        )
    )
    await session.commit()
    return result.rowcount > 0
