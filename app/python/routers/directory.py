"""`/api/v1/directory` — profile directory (discoverable accounts).

Orders by recent status activity (`active`, default) or account creation
time (`new`). Supports `?local` to restrict to local-origin accounts.
Requires the `profile_directory` setting to be enabled.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import joinedload

from app.python.deps import DBSession, OptionalAuth
from app.python.models import Account
from app.python.models.account_stat import AccountStat
from app.python.schemas.account import Account_, serialize_account

router = APIRouter(tags=["directory"])


@router.get("/api/v1/directory", response_model=list[Account_])
async def directory(
    session: DBSession,
    auth: OptionalAuth,
    order: str = Query(default="active"),
    local: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=40, ge=1, le=80),
) -> list[Account_]:
    stmt = (
        select(Account)
        .outerjoin(AccountStat, AccountStat.account_id == Account.id)
        .options(joinedload(Account.stat))
        .where(
            Account.discoverable.is_(True),
            Account.suspended_at.is_(None),
        )
    )

    if local:
        stmt = stmt.where(Account.domain.is_(None))
    else:
        stmt = stmt.where(Account.moved_to_account_id.is_(None))

    if order == "new":
        stmt = stmt.order_by(desc(Account.id))
    else:
        stmt = stmt.order_by(desc(AccountStat.last_status_at).nulls_last(), desc(Account.id))

    stmt = stmt.offset(offset).limit(limit)
    accounts = (await session.execute(stmt)).unique().scalars().all()

    return [serialize_account(a) for a in accounts]
