"""Maintain Conversation + AccountConversation rows when a DM is posted.

Mastodon's model for DMs: a `Conversation` row groups DIRECT-visibility
statuses with the same set of participants. Each participant gets their
own `AccountConversation` row mapping the conversation to their view of
it (which statuses they can see in this thread, unread flag, etc.).

Threading: if the new status is a reply to a status that already has a
conversation_id, reuse that conversation. Otherwise create a new one
with `parent_status_id = new_status.id`. We don't yet hash on the
participant set to dedupe brand-new threads between the same people —
that's an optimization the legacy backend uses to keep the conversation
list short, deferred here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import now_id
from app.python.models import (
    AccountConversation,
    Conversation,
    Mention,
    Status,
)


async def attach_to_conversation(
    session: AsyncSession,
    *,
    status: Status,
    author_id: int,
    mentioned_account_ids: list[int],
    now: datetime,
) -> None:
    """Create/reuse a Conversation, write the status's conversation_id, and
    upsert AccountConversation rows for the author + each mentioned account.
    """
    conversation_id = await _resolve_conversation_id(
        session, status=status, author_id=author_id, now=now
    )
    status.conversation_id = conversation_id

    participants = sorted({author_id, *mentioned_account_ids})

    for account_id in participants:
        # Recipients view starts unread; author's own view is already read.
        unread = account_id != author_id
        await _upsert_account_conversation(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            participant_account_ids=[a for a in participants if a != account_id],
            new_status_id=status.id,
            unread=unread,
        )


async def _resolve_conversation_id(
    session: AsyncSession,
    *,
    status: Status,
    author_id: int,
    now: datetime,
) -> int:
    if status.in_reply_to_id is not None:
        parent = (
            await session.execute(
                select(Status.conversation_id).where(Status.id == status.in_reply_to_id)
            )
        ).scalar_one_or_none()
        if parent is not None:
            return parent

    convo = Conversation(
        id=now_id(),
        uri=None,
        parent_status_id=status.id,
        parent_account_id=author_id,
        created_at=now,
        updated_at=now,
    )
    session.add(convo)
    await session.flush()
    return convo.id


async def _upsert_account_conversation(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    participant_account_ids: list[int],
    new_status_id: int,
    unread: bool,
) -> None:
    existing = (
        await session.execute(
            select(AccountConversation).where(
                AccountConversation.account_id == account_id,
                AccountConversation.conversation_id == conversation_id,
                AccountConversation.participant_account_ids == participant_account_ids,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.status_ids = [*(existing.status_ids or []), new_status_id]
        existing.last_status_id = new_status_id
        existing.lock_version += 1
        if unread:
            existing.unread = True
        return

    session.add(
        AccountConversation(
            id=now_id(),
            account_id=account_id,
            conversation_id=conversation_id,
            participant_account_ids=participant_account_ids,
            status_ids=[new_status_id],
            last_status_id=new_status_id,
            lock_version=0,
            unread=unread,
        )
    )


async def mentioned_account_ids_for(
    session: AsyncSession, status_id: int
) -> list[int]:
    """Return the account ids on Mention rows for a status."""
    rows = (
        await session.execute(
            select(Mention.account_id).where(Mention.status_id == status_id)
        )
    ).scalars().all()
    return list(rows)
