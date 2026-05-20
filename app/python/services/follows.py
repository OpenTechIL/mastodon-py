"""Follow / unfollow services.

Decisions this slice locks in:

  - **Self-follow → not found.** Mirrors the Rails `following_not_possible?`
    short-circuit; the caller surfaces it as 404.
  - **Locked target → FollowRequest.** Posts are still hidden until the
    target accepts. No counter moves until promotion. Authorize/reject
    endpoints land in their own slice.
  - **Counter moves on real Follow only.** `account_stats.following_count`
    on the actor and `account_stats.followers_count` on the target both
    move on follow/unfollow. FollowRequest leaves counters untouched.
  - **Re-following a target that already has a FollowRequest is idempotent**
    — return the existing request, do not duplicate.

Deferred to their owning phases:

  - Block / mute checks. The Rails service refuses follow when either
    side has a block; until blocks port, we let the follow create.
  - AP `Follow` / `Undo Follow` delivery to remote inboxes.
  - Local notifications on follow / follow-request to the target.
  - Home-feed merge (pulling target's recent statuses into the
    follower's home feed). Ports with the Redis fan-out phase.
  - The `with_redis_lock("relationship:<a>:<b>")` cross-process serializer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.counter_cache import adjust_counter
from app.python.common.snowflake import now_id
from app.python.federation.keys import ensure_local_actor_keys
from app.python.lib.asset_urls import account_uri
from app.python.models import Account, Block, Follow, FollowRequest, NotificationType
from app.python.queue import Enqueuer
from app.python.services.notifications import create_local as create_notification


class SelfFollow(Exception):
    """Raised when an account tries to follow itself."""


class BlockedFollow(Exception):
    """Raised when a follow is refused because of a Block in either direction."""


async def _blocked_either_way(
    session: AsyncSession, a: int, b: int
) -> bool:
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


async def _existing_follow(
    session: AsyncSession, source: Account, target: Account
) -> Follow | None:
    return (
        await session.execute(
            select(Follow).where(
                Follow.account_id == source.id,
                Follow.target_account_id == target.id,
            )
        )
    ).scalar_one_or_none()


async def _existing_request(
    session: AsyncSession, source: Account, target: Account
) -> FollowRequest | None:
    return (
        await session.execute(
            select(FollowRequest).where(
                FollowRequest.account_id == source.id,
                FollowRequest.target_account_id == target.id,
            )
        )
    ).scalar_one_or_none()


async def follow(
    session: AsyncSession,
    *,
    source: Account,
    target: Account,
    enqueuer: Enqueuer | None = None,
) -> Follow | FollowRequest:
    if source.id == target.id:
        raise SelfFollow
    if await _blocked_either_way(session, source.id, target.id):
        raise BlockedFollow

    existing = await _existing_follow(session, source, target)
    if existing is not None:
        return existing
    pending = await _existing_request(session, source, target)
    if pending is not None:
        return pending

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    follow_id = now_id()
    follow_uri = (
        f"{account_uri(source)}#follows/{follow_id}" if source.local else None
    )
    if target.locked:
        req = FollowRequest(
            id=follow_id,
            account_id=source.id,
            target_account_id=target.id,
            show_reblogs=True,
            notify=False,
            languages=None,
            uri=follow_uri,
            created_at=now,
            updated_at=now,
        )
        session.add(req)
        await session.flush()
        await create_notification(
            session,
            recipient=target,
            actor=source,
            activity_id=req.id,
            type=NotificationType.FOLLOW_REQUEST,
        )
        await session.commit()
        await _enqueue_outbound_follow(
            session, enqueuer, source=source, target=target, follow_uri=follow_uri
        )
        return req

    follow_row = Follow(
        id=follow_id,
        account_id=source.id,
        target_account_id=target.id,
        show_reblogs=True,
        notify=False,
        languages=None,
        uri=follow_uri,
        created_at=now,
        updated_at=now,
    )
    session.add(follow_row)
    await session.flush()

    await adjust_counter(
        session,
        table="account_stats",
        row_id=source.id,
        column="following_count",
        delta=1,
    )
    await adjust_counter(
        session,
        table="account_stats",
        row_id=target.id,
        column="followers_count",
        delta=1,
    )
    await create_notification(
        session,
        recipient=target,
        actor=source,
        activity_id=follow_row.id,
        type=NotificationType.FOLLOW,
    )
    await session.commit()
    await _enqueue_outbound_follow(
        session, enqueuer, source=source, target=target, follow_uri=follow_uri
    )
    return follow_row


async def _enqueue_outbound_follow(
    session: AsyncSession,
    enqueuer: Enqueuer | None,
    *,
    source: Account,
    target: Account,
    follow_uri: str | None,
) -> None:
    """Build + enqueue a Follow activity when the source is local and
    the target is remote.

    No-op when:
      - enqueuer is None (caller didn't wire deps; tests for the
        in-DB-only path).
      - source is remote (we aren't authoritative for their follows).
      - target is local (no remote inbox to deliver to).
      - target has no inbox URL.
    """
    if enqueuer is None or not source.local or target.local:
        return
    inbox = (target.shared_inbox_url or "").strip() or (target.inbox_url or "").strip()
    if not inbox:
        return
    if not source.private_key:
        await asyncio.to_thread(ensure_local_actor_keys, source)
        await session.commit()
    source_uri = account_uri(source)
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": follow_uri or f"{source_uri}#follows/{now_id()}",
        "type": "Follow",
        "actor": source_uri,
        "object": account_uri(target),
    }
    await enqueuer.enqueue("deliver_activity", activity, source.id, [inbox])


async def authorize_follow_request(
    session: AsyncSession,
    *,
    target: Account,
    requester: Account,
) -> Follow | None:
    """Promote a pending `FollowRequest(requester -> target)` into a Follow.

    Counters move at this point (the follow is only "real" once accepted).
    A follow-type notification fires to the requester so they see the
    accept land in their notifications column. Returns None if there's no
    matching request.
    """
    req = await _existing_request(session, requester, target)
    if req is None:
        return None

    await session.execute(
        delete(FollowRequest).where(FollowRequest.id == req.id)
    )

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    follow_row = Follow(
        id=now_id(),
        account_id=requester.id,
        target_account_id=target.id,
        show_reblogs=req.show_reblogs,
        notify=req.notify,
        languages=req.languages,
        uri=req.uri,
        created_at=now,
        updated_at=now,
    )
    session.add(follow_row)
    await session.flush()

    await adjust_counter(
        session,
        table="account_stats",
        row_id=requester.id,
        column="following_count",
        delta=1,
    )
    await adjust_counter(
        session,
        table="account_stats",
        row_id=target.id,
        column="followers_count",
        delta=1,
    )
    # Tell the requester their follow landed. (Recipient is `requester`,
    # actor is `target` — the acceptance came from the locked account.)
    await create_notification(
        session,
        recipient=requester,
        actor=target,
        activity_id=follow_row.id,
        type=NotificationType.FOLLOW,
    )
    await session.commit()
    return follow_row


async def reject_follow_request(
    session: AsyncSession,
    *,
    target: Account,
    requester: Account,
) -> bool:
    result = await session.execute(
        delete(FollowRequest).where(
            FollowRequest.account_id == requester.id,
            FollowRequest.target_account_id == target.id,
        )
    )
    if not result.rowcount:
        return False
    await session.commit()
    return True


async def unfollow(
    session: AsyncSession,
    *,
    source: Account,
    target: Account,
    enqueuer: Enqueuer | None = None,
) -> bool:
    """Return True if a Follow or pending FollowRequest was removed."""
    # Capture the follow uri before deletion so we can cite it in the
    # outbound Undo. Mastodon's contract: peers correlate the Undo
    # with the original Follow by the URI we stamped on it.
    existing_uri: str | None = None
    if source.local and not target.local:
        existing_follow = (
            await session.execute(
                select(Follow).where(
                    Follow.account_id == source.id,
                    Follow.target_account_id == target.id,
                )
            )
        ).scalar_one_or_none()
        if existing_follow is not None:
            existing_uri = existing_follow.uri
        else:
            existing_req = (
                await session.execute(
                    select(FollowRequest).where(
                        FollowRequest.account_id == source.id,
                        FollowRequest.target_account_id == target.id,
                    )
                )
            ).scalar_one_or_none()
            if existing_req is not None:
                existing_uri = existing_req.uri

    follow_result = await session.execute(
        delete(Follow).where(
            Follow.account_id == source.id,
            Follow.target_account_id == target.id,
        )
    )
    if follow_result.rowcount:
        await adjust_counter(
            session,
            table="account_stats",
            row_id=source.id,
            column="following_count",
            delta=-1,
        )
        await adjust_counter(
            session,
            table="account_stats",
            row_id=target.id,
            column="followers_count",
            delta=-1,
        )
        await session.commit()
        await _enqueue_outbound_undo_follow(
            session, enqueuer, source=source, target=target, follow_uri=existing_uri
        )
        return True

    request_result = await session.execute(
        delete(FollowRequest).where(
            FollowRequest.account_id == source.id,
            FollowRequest.target_account_id == target.id,
        )
    )
    if request_result.rowcount:
        await session.commit()
        await _enqueue_outbound_undo_follow(
            session, enqueuer, source=source, target=target, follow_uri=existing_uri
        )
        return True
    return False


async def _enqueue_outbound_undo_follow(
    session: AsyncSession,
    enqueuer: Enqueuer | None,
    *,
    source: Account,
    target: Account,
    follow_uri: str | None,
) -> None:
    if enqueuer is None or not source.local or target.local:
        return
    inbox = (target.shared_inbox_url or "").strip() or (target.inbox_url or "").strip()
    if not inbox:
        return
    if not source.private_key:
        await asyncio.to_thread(ensure_local_actor_keys, source)
        await session.commit()
    source_uri = account_uri(source)
    inner_follow = {
        "id": follow_uri or f"{source_uri}#follows/unknown",
        "type": "Follow",
        "actor": source_uri,
        "object": account_uri(target),
    }
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{follow_uri}/undo" if follow_uri else f"{source_uri}#undo/{now_id()}",
        "type": "Undo",
        "actor": source_uri,
        "object": inner_follow,
    }
    await enqueuer.enqueue("deliver_activity", activity, source.id, [inbox])
