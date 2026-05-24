"""Edit an existing status.

Mastodon enforces a few invariants on edit:

  - **Visibility is immutable.** Federation peers cache the visibility
    with the original Create; an edit that flipped it from public to
    private would be honored locally but never propagated.
  - **`in_reply_to_id` is immutable.** Same reason — the thread graph
    is fixed once the Status is published.
  - **Author-only.** Even with admin privileges, the legacy backend
    refuses; we mirror that and let admin-side edits land in the admin
    phase.
  - **Snapshot before mutate.** Each edit appends a `StatusEdit` row
    capturing the pre-edit state; the history endpoint walks these.
  - **`update` notifications.** Local accounts who reblogged the
    edited status receive a notification of type `update`.

Deferred:

  - AP `Update` activity delivery to remote followers.
  - Media attachment / poll mutation — relies on those models porting.
  - Re-running mention / hashtag extraction on the new text.
  - Resetting the preview-card / quote-approval workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import now_id
from app.python.models import Account, Status, StatusEdit
from app.python.models.notification import NotificationType
from app.python.services.notifications import create_local
from app.python.services.streaming import publish_notification


class StatusForbidden(Exception):
    """Raised when the caller is not the author."""


@dataclass(slots=True)
class StatusUpdate:
    text: str | None = None
    spoiler_text: str | None = None
    sensitive: bool | None = None
    language: str | None = None


def _unchanged(status: Status, update: StatusUpdate) -> bool:
    if update.text is not None and update.text != status.text:
        return False
    if update.spoiler_text is not None and update.spoiler_text != status.spoiler_text:
        return False
    if update.sensitive is not None and update.sensitive != status.sensitive:
        return False
    if update.language is not None and update.language != status.language:
        return False
    return True


async def update_status(
    session: AsyncSession,
    *,
    author: Account,
    status: Status,
    update: StatusUpdate,
) -> Status:
    if status.account_id != author.id:
        raise StatusForbidden
    if _unchanged(status, update):
        return status

    now = datetime.now(tz=UTC).replace(tzinfo=None)

    # Snapshot the pre-edit state.
    snapshot = StatusEdit(
        id=now_id(),
        status_id=status.id,
        account_id=author.id,
        text=status.text,
        spoiler_text=status.spoiler_text,
        sensitive=status.sensitive,
        ordered_media_attachment_ids=None,
        media_descriptions=None,
        poll_options=None,
        quote_id=None,
        created_at=now,
        updated_at=now,
    )
    session.add(snapshot)

    # Apply the edit. Only update fields the caller explicitly provided
    # so PATCH-style partial updates work via the same endpoint.
    if update.text is not None:
        status.text = update.text
    if update.spoiler_text is not None:
        status.spoiler_text = update.spoiler_text
        if update.spoiler_text.strip():
            status.sensitive = True
    if update.sensitive is not None:
        status.sensitive = update.sensitive
    if update.language is not None:
        status.language = update.language

    status.edited_at = now
    status.updated_at = now

    await session.commit()

    # Notify local rebloggers about the edit.
    await _notify_rebloggers(session, author=author, status=status)

    return status


async def _notify_rebloggers(
    session: AsyncSession,
    *,
    author: Account,
    status: Status,
) -> None:
    """Send `update` notifications to local accounts who reblogged this status."""
    # Find distinct local accounts that boosted this status (not yet soft-deleted).
    boost_accounts = (
        await session.execute(
            select(Account)
            .join(Status, Status.account_id == Account.id)
            .where(
                Status.reblog_of_id == status.id,
                Status.deleted_at.is_(None),
                Account.domain.is_(None),  # local accounts only
            )
            .distinct()
        )
    ).scalars().all()

    pending: list[tuple[int, int]] = []
    for recipient in boost_accounts:
        notif = await create_local(
            session,
            recipient=recipient,
            actor=author,
            activity_id=status.id,
            type=NotificationType.UPDATE,
        )
        if notif:
            pending.append((notif.id, recipient.id))

    if boost_accounts:
        await session.commit()
        for notif_id, recipient_id in pending:
            await publish_notification(notif_id, recipient_id)
