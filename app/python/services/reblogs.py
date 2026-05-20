"""Reblog / unreblog services.

Asymmetric with favourite/bookmark because a reblog is itself a Status
row — same `statuses` table, with `reblog_of_id` pointing back at the
parent. That has two consequences this module owns:

  - **Two counters move at once.** Boosting bumps `status_stats.reblogs_count`
    on the parent AND `account_stats.statuses_count` on the booster
    (the boost is one of the booster's statuses). Undoing reverses both.
  - **Chain traversal.** Reblogging a reblog must resolve to the root —
    `account.boosts(boost_of_alice_status)` always points at alice's
    original. Without this the boost graph fans out into a forest.

Deferred to their owning phases:

  - Notifications and AP `Announce` delivery to the original author.
  - Trends registration.
  - The `with_redis_lock("reblog:<viewer>:<status>")` the legacy
    controller wraps the create with. Until the write-side fan-out
    phase introduces redis-py-locked code paths we rely on the unique
    behavior of the (account_id, reblog_of_id) pair (enforced at the
    application layer for now — no DB unique index on it).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.counter_cache import adjust_counter
from app.python.common.snowflake import now_id
from app.python.federation.fanout import collect_inbox_urls
from app.python.federation.keys import ensure_local_actor_keys
from app.python.lib.asset_urls import account_uri
from app.python.models import Account, Follow, NotificationType, Status, Visibility
from app.python.policies.status_policy import visible_to
from app.python.queue import Enqueuer
from app.python.services.favourites import StatusNotFound
from app.python.services.notifications import create_local as create_notification


def _root_of(status: Status) -> Status:
    """If `status` is itself a boost, follow it back to the root status."""
    return status.reblog if status.reblog is not None else status


def _can_reblog(parent: Status) -> bool:
    vis = Visibility(parent.visibility)
    return vis in (Visibility.PUBLIC, Visibility.UNLISTED)


async def reblog(
    session: AsyncSession,
    *,
    account: Account,
    status: Status,
    visibility: Visibility = Visibility.PUBLIC,
    enqueuer: Enqueuer | None = None,
) -> Status:
    """Create or return the existing boost of `status` by `account`.

    Returns the wrapper Status (the boost row). Caller serializes it;
    the nested `reblog` field on the response will be the resolved root.
    """
    parent = _root_of(status)
    if not await visible_to(session, parent, account.id) or not _can_reblog(parent):
        raise StatusNotFound

    existing = (
        await session.execute(
            select(Status).where(
                Status.account_id == account.id,
                Status.reblog_of_id == parent.id,
                Status.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    wrapper_id = now_id()
    wrapper_uri: str | None = None
    if account.local:
        wrapper_uri = f"{account_uri(account)}/statuses/{wrapper_id}/activity"
    wrapper = Status(
        id=wrapper_id,
        account_id=account.id,
        text="",
        spoiler_text="",
        sensitive=False,
        visibility=visibility.value,
        language=None,
        local=True,
        reply=False,
        reblog_of_id=parent.id,
        uri=wrapper_uri,
        url=None,
        created_at=now,
        updated_at=now,
    )
    session.add(wrapper)
    await session.flush()

    await adjust_counter(
        session,
        table="status_stats",
        row_id=parent.id,
        column="reblogs_count",
        delta=1,
    )
    await adjust_counter(
        session,
        table="account_stats",
        row_id=account.id,
        column="statuses_count",
        delta=1,
    )
    await create_notification(
        session,
        recipient=parent.account,
        actor=account,
        activity_id=wrapper.id,
        type=NotificationType.REBLOG,
    )
    await session.commit()
    await _enqueue_outbound_announce(session, enqueuer, source=account, wrapper=wrapper, parent=parent)
    return wrapper


async def unreblog(
    session: AsyncSession,
    *,
    account: Account,
    status: Status,
    enqueuer: Enqueuer | None = None,
) -> bool:
    """Discard `account`'s boost of `status`. Returns True if a row was removed.

    Reblog rows are soft-deleted (Discardable) so federation can still
    emit an `Undo Announce` referencing the original id when delivery
    ports. Hard-deletion would lose that reference.
    """
    parent = _root_of(status)
    wrapper = (
        await session.execute(
            select(Status).where(
                Status.account_id == account.id,
                Status.reblog_of_id == parent.id,
                Status.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if wrapper is None:
        return False

    wrapper_uri = wrapper.uri
    wrapper.discard()
    await session.flush()

    await adjust_counter(
        session,
        table="status_stats",
        row_id=parent.id,
        column="reblogs_count",
        delta=-1,
    )
    await adjust_counter(
        session,
        table="account_stats",
        row_id=account.id,
        column="statuses_count",
        delta=-1,
    )
    await session.commit()
    await _enqueue_outbound_undo_announce(session, enqueuer, source=account, parent=parent, wrapper_uri=wrapper_uri)
    return True


async def _collect_announce_inboxes(session: AsyncSession, *, source: Account, parent: Status) -> list[str]:
    """Audience: source's remote followers + parent author's inbox
    (when remote). Deduped via `collect_inbox_urls`."""
    rows = (
        (
            await session.execute(
                select(Account)
                .join(Follow, Follow.account_id == Account.id)
                .where(
                    Follow.target_account_id == source.id,
                    Account.domain.is_not(None),
                )
            )
        )
        .unique()
        .scalars()
        .all()
    )
    # Include parent author so they get the notification.
    accounts: list[Account] = list(rows)
    author = parent.account
    if author is not None and not author.local:
        accounts.append(author)
    return collect_inbox_urls(accounts)


async def _enqueue_outbound_announce(
    session: AsyncSession,
    enqueuer: Enqueuer | None,
    *,
    source: Account,
    wrapper: Status,
    parent: Status,
) -> None:
    if enqueuer is None or not source.local or not parent.uri:
        return
    inbox_urls = await _collect_announce_inboxes(session, source=source, parent=parent)
    if not inbox_urls:
        return
    if not source.private_key:
        await asyncio.to_thread(ensure_local_actor_keys, source)
        await session.commit()
    source_uri = account_uri(source)
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": wrapper.uri or f"{source_uri}#announces/{wrapper.id}",
        "type": "Announce",
        "actor": source_uri,
        "object": parent.uri,
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "cc": [f"{source_uri}/followers"],
        "published": wrapper.created_at.isoformat(timespec="seconds") + "Z",
    }
    await enqueuer.enqueue("deliver_activity", activity, source.id, inbox_urls)


async def _enqueue_outbound_undo_announce(
    session: AsyncSession,
    enqueuer: Enqueuer | None,
    *,
    source: Account,
    parent: Status,
    wrapper_uri: str | None,
) -> None:
    if enqueuer is None or not source.local or not parent.uri:
        return
    inbox_urls = await _collect_announce_inboxes(session, source=source, parent=parent)
    if not inbox_urls:
        return
    if not source.private_key:
        await asyncio.to_thread(ensure_local_actor_keys, source)
        await session.commit()
    source_uri = account_uri(source)
    inner = {
        "id": wrapper_uri or f"{source_uri}#announces/unknown",
        "type": "Announce",
        "actor": source_uri,
        "object": parent.uri,
    }
    activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{wrapper_uri}/undo" if wrapper_uri else f"{source_uri}#undo/{now_id()}",
        "type": "Undo",
        "actor": source_uri,
        "object": inner,
    }
    await enqueuer.enqueue("deliver_activity", activity, source.id, inbox_urls)
