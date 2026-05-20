"""Soft-delete a status authored by the calling account.

Deferred to dedicated phases:

  - `RemovalWorker` chain — federated Delete delivery, retraction of
    notifications, removal from home feeds, deletion of media files.
  - Self-delete of reblogs (handled here by symmetry with reblog_service)
    is in scope; deleting OTHER accounts' reblogs of this status is the
    federation pipeline's job.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.counter_cache import adjust_counter
from app.python.models import Account, Status


class StatusForbidden(Exception):
    """Raised when the caller is not the author of the status."""


async def delete_status(
    session: AsyncSession,
    *,
    author: Account,
    status: Status,
) -> Status:
    if status.account_id != author.id:
        raise StatusForbidden
    if status.discarded:
        return status

    status.discard()
    await session.flush()

    await adjust_counter(
        session,
        table="account_stats",
        row_id=author.id,
        column="statuses_count",
        delta=-1,
    )
    if status.reblog_of_id is not None:
        # Deleting a reblog wrapper decrements the original's count.
        await adjust_counter(
            session,
            table="status_stats",
            row_id=status.reblog_of_id,
            column="reblogs_count",
            delta=-1,
        )
    elif status.in_reply_to_id is not None:
        # Deleting a reply decrements the parent's reply count.
        await adjust_counter(
            session,
            table="status_stats",
            row_id=status.in_reply_to_id,
            column="replies_count",
            delta=-1,
        )

    await session.commit()

    from app.python.services.streaming import publish_delete

    await publish_delete(status.id, author.id)

    return status
