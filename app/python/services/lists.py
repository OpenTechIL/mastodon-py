"""List CRUD + list membership.

A list is wholly owned by the account that created it. Membership is
constrained: you can only add accounts you currently follow. Removing
no longer follows nothing — the row just goes away. The Rails
`UnfollowService` additionally drops list memberships when the follow
ends; that cascade ports with the unfollow service when the list model
is referenced there.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status as http_status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import now_id
from app.python.models import (
    Account,
    Follow,
    List,
    ListAccount,
    RepliesPolicy,
    parse_replies_policy,
)


class ListNotFound(Exception):
    """Raised when a list doesn't exist for the calling account."""


class NotFollowing(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Accounts in a list must be ones you follow",
        )


async def _list_for(
    session: AsyncSession, owner: Account, list_id: int
) -> List:
    row = (
        await session.execute(
            select(List).where(List.id == list_id, List.account_id == owner.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ListNotFound
    return row


async def list_lists(session: AsyncSession, owner: Account) -> list[List]:
    return list(
        (
            await session.execute(
                select(List)
                .where(List.account_id == owner.id)
                .order_by(List.id.asc())
            )
        ).scalars().all()
    )


async def create_list(
    session: AsyncSession,
    *,
    owner: Account,
    title: str,
    replies_policy: str | None = None,
    exclusive: bool = False,
) -> List:
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    row = List(
        id=now_id(),
        account_id=owner.id,
        title=title,
        replies_policy=parse_replies_policy(replies_policy).value,
        exclusive=exclusive,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def update_list(
    session: AsyncSession,
    *,
    owner: Account,
    list_id: int,
    title: str | None = None,
    replies_policy: str | None = None,
    exclusive: bool | None = None,
) -> List:
    row = await _list_for(session, owner, list_id)
    if title is not None:
        row.title = title
    if replies_policy is not None:
        row.replies_policy = parse_replies_policy(replies_policy).value
    if exclusive is not None:
        row.exclusive = exclusive
    row.updated_at = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    await session.commit()
    return row


async def delete_list(
    session: AsyncSession,
    *,
    owner: Account,
    list_id: int,
) -> None:
    await _list_for(session, owner, list_id)  # 404 if missing
    await session.execute(delete(ListAccount).where(ListAccount.list_id == list_id))
    await session.execute(delete(List).where(List.id == list_id))
    await session.commit()


async def list_members(
    session: AsyncSession,
    *,
    owner: Account,
    list_id: int,
) -> list[Account]:
    await _list_for(session, owner, list_id)
    rows = (
        await session.execute(
            select(Account)
            .join(ListAccount, ListAccount.account_id == Account.id)
            .where(ListAccount.list_id == list_id)
            .order_by(ListAccount.id.desc())
        )
    ).unique().scalars().all()
    return list(rows)


async def add_accounts(
    session: AsyncSession,
    *,
    owner: Account,
    list_id: int,
    account_ids: list[int],
) -> None:
    list_row = await _list_for(session, owner, list_id)
    if not account_ids:
        return

    # Every account must be one the owner follows.
    follow_pairs = (
        await session.execute(
            select(Follow.target_account_id, Follow.id).where(
                Follow.account_id == owner.id,
                Follow.target_account_id.in_(account_ids),
            )
        )
    ).all()
    follow_id_for = {tid: fid for tid, fid in follow_pairs}
    missing = [a for a in account_ids if a not in follow_id_for]
    if missing:
        raise NotFollowing()

    # Skip already-listed accounts to keep the call idempotent.
    existing = set(
        (
            await session.execute(
                select(ListAccount.account_id).where(
                    ListAccount.list_id == list_row.id,
                    ListAccount.account_id.in_(account_ids),
                )
            )
        ).scalars().all()
    )
    for aid in account_ids:
        if aid in existing:
            continue
        session.add(
            ListAccount(
                id=now_id(),
                list_id=list_row.id,
                account_id=aid,
                follow_id=follow_id_for[aid],
                follow_request_id=None,
            )
        )
    await session.commit()


async def remove_accounts(
    session: AsyncSession,
    *,
    owner: Account,
    list_id: int,
    account_ids: list[int],
) -> None:
    await _list_for(session, owner, list_id)
    if not account_ids:
        return
    await session.execute(
        delete(ListAccount).where(
            ListAccount.list_id == list_id,
            ListAccount.account_id.in_(account_ids),
        )
    )
    await session.commit()
