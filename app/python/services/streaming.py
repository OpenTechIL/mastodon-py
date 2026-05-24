"""Publish status events to Redis so the streaming server pushes live updates.

The streaming server (Node.js) subscribes to Redis pub/sub channels named:
  timeline:public             — all public statuses
  timeline:public:local       — local-origin public statuses
  timeline:{account_id}       — home timeline for each follower
  timeline:{account_id}:notifications  — (notifications, handled separately)

Messages are JSON-encoded: {"event": "update"|"delete"|…, "payload": ...}
where `payload` for `update` is the full serialised status JSON string.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.models import Account, Follow, Status, Visibility

logger = logging.getLogger(__name__)


def _redis_client():
    """Return a synchronous-compatible async Redis client."""
    import redis.asyncio as redis  # imported lazily so tests without Redis still work

    from app.python.settings import get_settings

    s = get_settings()
    return redis.Redis(host=s.redis_host, port=s.redis_port, password=s.redis_password)


def _ns(channel: str, namespace: str | None) -> str:
    return f"{namespace}:{channel}" if namespace else channel


async def publish_status(
    session: AsyncSession,
    status: Status,
    author: Account,
) -> None:
    """Publish `update` event to all relevant Redis timeline channels."""
    if not author.local:
        return
    vis = status.visibility
    if vis not in (Visibility.PUBLIC.value, Visibility.UNLISTED.value):
        return

    from app.python.schemas.status import serialize_status
    from app.python.settings import get_settings

    s = get_settings()
    ns = s.redis_namespace

    try:
        # Refresh the status to eagerly load stat relationship before serializing.
        await session.refresh(status, attribute_names=["stat"])
        payload_str = serialize_status(status).model_dump_json()
        message = json.dumps({"event": "update", "payload": payload_str})

        r = _redis_client()
        async with r:
            pipe = r.pipeline()
            if vis == Visibility.PUBLIC.value:
                pipe.publish(_ns("timeline:public", ns), message)
                pipe.publish(_ns("timeline:public:local", ns), message)
            elif vis == Visibility.UNLISTED.value:
                # Unlisted posts appear on home feeds but not the public timeline
                pass

            # Fan out to local followers' home timelines
            follower_ids = (
                (
                    await session.execute(
                        select(Follow.account_id)
                        .join(Account, Account.id == Follow.account_id)
                        .where(
                            Follow.target_account_id == author.id,
                            Account.domain.is_(None),  # local only
                        )
                    )
                )
                .scalars()
                .all()
            )

            for fid in follower_ids:
                pipe.publish(_ns(f"timeline:{fid}", ns), message)
            # Also push to author's own home timeline
            pipe.publish(_ns(f"timeline:{author.id}", ns), message)

            await pipe.execute()
    except Exception:
        logger.exception("Failed to publish status %s to Redis", status.id)


async def publish_notification(notification_id: int, account_id: int) -> None:
    """Publish a `notification` event to the account's notification channel."""
    from app.python.settings import get_settings

    s = get_settings()
    ns = s.redis_namespace
    message = json.dumps({"event": "notification", "payload": str(notification_id)})

    try:
        r = _redis_client()
        async with r:
            await r.publish(_ns(f"timeline:{account_id}:notifications", ns), message)
    except Exception:
        logger.exception("Failed to publish notification %s to Redis", notification_id)


async def publish_delete(status_id: int, account_id: int) -> None:
    """Publish `delete` event to home timeline channel."""
    from app.python.settings import get_settings

    s = get_settings()
    ns = s.redis_namespace
    message = json.dumps({"event": "delete", "payload": str(status_id)})

    try:
        r = _redis_client()
        async with r:
            pipe = r.pipeline()
            pipe.publish(_ns("timeline:public", ns), message)
            pipe.publish(_ns("timeline:public:local", ns), message)
            pipe.publish(_ns(f"timeline:{account_id}", ns), message)
            await pipe.execute()
    except Exception:
        logger.exception("Failed to publish delete %s to Redis", status_id)
