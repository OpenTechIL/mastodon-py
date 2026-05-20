"""Visibility/authorization rules for statuses.

Block check first (either direction → invisible); then visibility
matrix. DIRECT now consults the `mentions` table. PRIVATE/LIMITED is
follower-only OR mentioned (LIMITED still degrades to that — the
audience-circle tables aren't modeled yet).
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.models import Block, Follow, Mention, Status, Visibility


async def visible_to(
    session: AsyncSession,
    status: Status,
    viewer_account_id: int | None,
) -> bool:
    if status.discarded:
        return False

    if viewer_account_id is not None and viewer_account_id != status.account_id:
        if await _blocked_either_way(session, viewer_account_id, status.account_id):
            return False

    vis = Visibility(status.visibility)
    if vis in (Visibility.PUBLIC, Visibility.UNLISTED):
        return True
    if viewer_account_id is None:
        return False
    if status.account_id == viewer_account_id:
        return True

    if vis == Visibility.DIRECT:
        return await _mentioned(session, viewer_account_id, status.id)

    # PRIVATE / LIMITED (latter degraded): follower OR mentioned.
    if await _follows(session, viewer_account_id, status.account_id):
        return True
    return await _mentioned(session, viewer_account_id, status.id)


async def _follows(session: AsyncSession, follower_id: int, target_id: int) -> bool:
    row = (
        await session.execute(
            select(Follow.id)
            .where(
                Follow.account_id == follower_id,
                Follow.target_account_id == target_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _mentioned(session: AsyncSession, viewer_id: int, status_id: int) -> bool:
    row = (
        await session.execute(
            select(Mention.id)
            .where(
                Mention.status_id == status_id,
                Mention.account_id == viewer_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _blocked_either_way(session: AsyncSession, a: int, b: int) -> bool:
    row = (
        await session.execute(
            select(Block.id)
            .where(
                or_(
                    (Block.account_id == a) & (Block.target_account_id == b),
                    (Block.account_id == b) & (Block.target_account_id == a),
                )
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None
