"""Pin / unpin a status.

Rules:

  - Author-only (you can only pin your own statuses).
  - Public or unlisted visibility only — private posts on your profile
    would surface to logged-out viewers via the pinned section.
  - Maximum 5 pins per account, matching `StatusPinValidator::PIN_LIMIT`.
  - Idempotent: pinning an already-pinned status returns the existing row.

Notifications are not generated (pinning your own post doesn't notify
anyone). AP `Add` / `Remove` to the featured collection is the federation
side of pin/unpin; deferred to the federation phase.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import now_id
from app.python.models import Account, Status, StatusPin, Visibility

MAX_PINNED_STATUSES = 5


class StatusForbidden(Exception):
    """Caller is not the author of the status."""


class TooManyPins(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"You can only pin up to {MAX_PINNED_STATUSES} statuses",
        )


class UnpinnableVisibility(HTTPException):
    def __init__(self) -> None:
        super().__init__(
            status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only public or unlisted statuses can be pinned",
        )


async def pin(
    session: AsyncSession,
    *,
    author: Account,
    status: Status,
) -> StatusPin:
    if status.account_id != author.id:
        raise StatusForbidden
    if Visibility(status.visibility) not in (Visibility.PUBLIC, Visibility.UNLISTED):
        raise UnpinnableVisibility()

    existing = (
        await session.execute(
            select(StatusPin).where(
                StatusPin.account_id == author.id,
                StatusPin.status_id == status.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    count = (
        await session.execute(select(func.count()).select_from(StatusPin).where(StatusPin.account_id == author.id))
    ).scalar_one()
    if count >= MAX_PINNED_STATUSES:
        raise TooManyPins()

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = StatusPin(
        id=now_id(),
        account_id=author.id,
        status_id=status.id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def unpin(
    session: AsyncSession,
    *,
    author: Account,
    status: Status,
) -> bool:
    if status.account_id != author.id:
        raise StatusForbidden
    result = await session.execute(
        delete(StatusPin).where(
            StatusPin.account_id == author.id,
            StatusPin.status_id == status.id,
        )
    )
    if not result.rowcount:  # type: ignore[attr-defined]
        return False
    await session.commit()
    return True
