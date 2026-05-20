"""Block / unblock.

Blocking is a hard cut. The Rails service additionally:
  - destroys both-direction follows (and decrements counters)
  - rejects any pending follow request from the target
  - kicks the target out of the blocker's curated collections
  - deletes any NotificationPermission grant from the target
  - schedules a BlockWorker that prunes the blocker's home feed cache
  - federates the Block via ActivityPub

This slice handles the bits that have ported tables: the follow / follow
request cleanup. Collections, NotificationPermission, BlockWorker
(home-feed cache pruning), and AP delivery are deferred to their owning
phases. Pruning the home-feed cache isn't needed for correctness here
because our home timeline reads `statuses` directly; once the Redis
fan-out phase introduces a cached feed, that worker becomes load-bearing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.counter_cache import adjust_counter
from app.python.common.snowflake import now_id
from app.python.models import Account, Block, Follow, FollowRequest


class SelfBlock(Exception):
    """Raised when an account tries to block itself."""


async def _tear_down_follow(session: AsyncSession, follower_id: int, target_id: int) -> None:
    """Destroy a Follow and decrement the relevant counters.

    Inlined from `services.follows.unfollow` because the block side
    effect runs over both directions and the symmetry is easier to read
    inline than as two `await follow_service.unfollow(...)` calls (each
    of which would also commit independently).
    """
    result = await session.execute(
        delete(Follow).where(
            Follow.account_id == follower_id,
            Follow.target_account_id == target_id,
        )
    )
    if result.rowcount:  # type: ignore[attr-defined]
        await adjust_counter(
            session,
            table="account_stats",
            row_id=follower_id,
            column="following_count",
            delta=-1,
        )
        await adjust_counter(
            session,
            table="account_stats",
            row_id=target_id,
            column="followers_count",
            delta=-1,
        )


async def _tear_down_follow_request(session: AsyncSession, follower_id: int, target_id: int) -> None:
    await session.execute(
        delete(FollowRequest).where(
            FollowRequest.account_id == follower_id,
            FollowRequest.target_account_id == target_id,
        )
    )


async def block(
    session: AsyncSession,
    *,
    source: Account,
    target: Account,
) -> Block:
    if source.id == target.id:
        raise SelfBlock

    existing = (
        await session.execute(
            select(Block).where(
                Block.account_id == source.id,
                Block.target_account_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # Tear down both directions of the follow graph BEFORE the block is
    # written. Doing it in this order keeps the invariant "no Follow + Block
    # simultaneously between the same pair" enforceable by a single
    # cleanup query at any point.
    await _tear_down_follow(session, source.id, target.id)
    await _tear_down_follow(session, target.id, source.id)
    await _tear_down_follow_request(session, source.id, target.id)
    await _tear_down_follow_request(session, target.id, source.id)

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = Block(
        id=now_id(),
        account_id=source.id,
        target_account_id=target.id,
        uri=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def unblock(
    session: AsyncSession,
    *,
    source: Account,
    target: Account,
) -> bool:
    result = await session.execute(
        delete(Block).where(
            Block.account_id == source.id,
            Block.target_account_id == target.id,
        )
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        return False
    await session.commit()
    return True
