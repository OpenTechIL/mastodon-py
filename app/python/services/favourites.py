"""Favourite / unfavourite — the simplest write services.

What's intentionally absent in this slice:

  - **Notifications.** The legacy service enqueues `LocalNotificationWorker`
    so the favourited user sees the like in their notifications column.
    Notification fan-out lands in its own phase; favouriting a local
    post in dev today produces no notification row.
  - **ActivityPub Like / Undo Like delivery.** The legacy service signs
    and POSTs a `Like` to the remote inbox when the favourited account
    is on another instance. Federation ports separately; dev favourites
    on remote posts are local-only until then.
  - **Trends registration.** `Trends.statuses.register(status)` ports
    with the rest of the trending pipeline.

What this slice DOES do is the database invariant that every other
piece depends on: a single (account_id, status_id) Favourite row exists
xor doesn't, and `status_stats.favourites_count` matches the row count.
The legacy service relied on an `after_create` callback to bump the
counter; we do it explicitly so the path is greppable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.counter_cache import adjust_counter
from app.python.common.snowflake import now_id
from app.python.federation.keys import ensure_local_actor_keys
from app.python.lib.asset_urls import account_uri
from app.python.models import Account, Favourite, NotificationType, Status
from app.python.policies.status_policy import visible_to
from app.python.queue import Enqueuer
from app.python.services.notifications import create_local as create_notification


class StatusNotFound(Exception):
    """Raised when the status cannot be favourited (missing or invisible)."""


async def favourite(
    session: AsyncSession,
    *,
    account: Account,
    status: Status,
    enqueuer: Enqueuer | None = None,
) -> Favourite:
    if not await visible_to(session, status, account.id):
        raise StatusNotFound

    existing = (
        await session.execute(
            select(Favourite).where(
                Favourite.account_id == account.id,
                Favourite.status_id == status.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = Favourite(
        id=now_id(),
        account_id=account.id,
        status_id=status.id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()

    await adjust_counter(
        session,
        table="status_stats",
        row_id=status.id,
        column="favourites_count",
        delta=1,
    )
    await create_notification(
        session,
        recipient=status.account,
        actor=account,
        activity_id=row.id,
        type=NotificationType.FAVOURITE,
    )
    await session.commit()
    await _enqueue_outbound_like(
        session, enqueuer, source=account, status=status, favourite_id=row.id
    )
    return row


async def unfavourite(
    session: AsyncSession,
    *,
    account: Account,
    status: Status,
    enqueuer: Enqueuer | None = None,
) -> bool:
    """Return True if a row was removed, False if there was nothing to remove."""
    # Capture the favourite id before delete so we can cite the original
    # Like's URI in the outbound Undo. Peers correlate Undo Like by
    # (actor, status), but emitting the id keeps wire-shape parity.
    existing_id: int | None = None
    if account.local and status.account is not None and not status.account.local:
        row = (
            await session.execute(
                select(Favourite).where(
                    Favourite.account_id == account.id,
                    Favourite.status_id == status.id,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            existing_id = row.id

    result = await session.execute(
        delete(Favourite).where(
            Favourite.account_id == account.id,
            Favourite.status_id == status.id,
        )
    )
    if result.rowcount == 0:
        return False
    await adjust_counter(
        session,
        table="status_stats",
        row_id=status.id,
        column="favourites_count",
        delta=-1,
    )
    await session.commit()
    await _enqueue_outbound_undo_like(
        session, enqueuer, source=account, status=status, favourite_id=existing_id
    )
    return True


async def _enqueue_outbound_like(
    session: AsyncSession,
    enqueuer: Enqueuer | None,
    *,
    source: Account,
    status: Status,
    favourite_id: int,
) -> None:
    """Federate a Like to a remote status's author.

    No-op when source is remote (we aren't authoritative), the status's
    author is local (no federation needed), or the author lacks an
    inbox URL.
    """
    if enqueuer is None or not source.local:
        return
    author = status.account
    if author is None or author.local:
        return
    inbox = (author.shared_inbox_url or "").strip() or (author.inbox_url or "").strip()
    if not inbox or not status.uri:
        return
    if not source.private_key:
        await asyncio.to_thread(ensure_local_actor_keys, source)
        await session.commit()
    source_uri = account_uri(source)
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{source_uri}#likes/{favourite_id}",
        "type": "Like",
        "actor": source_uri,
        "object": status.uri,
    }
    await enqueuer.enqueue("deliver_activity", activity, source.id, [inbox])


async def _enqueue_outbound_undo_like(
    session: AsyncSession,
    enqueuer: Enqueuer | None,
    *,
    source: Account,
    status: Status,
    favourite_id: int | None,
) -> None:
    if enqueuer is None or not source.local:
        return
    author = status.account
    if author is None or author.local:
        return
    inbox = (author.shared_inbox_url or "").strip() or (author.inbox_url or "").strip()
    if not inbox or not status.uri:
        return
    if not source.private_key:
        await asyncio.to_thread(ensure_local_actor_keys, source)
        await session.commit()
    source_uri = account_uri(source)
    like_uri = (
        f"{source_uri}#likes/{favourite_id}"
        if favourite_id is not None
        else f"{source_uri}#likes/unknown"
    )
    inner = {
        "id": like_uri,
        "type": "Like",
        "actor": source_uri,
        "object": status.uri,
    }
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{like_uri}/undo",
        "type": "Undo",
        "actor": source_uri,
        "object": inner,
    }
    await enqueuer.enqueue("deliver_activity", activity, source.id, [inbox])
