"""`/api/v1/follow_requests` — list, authorize, reject.

Lists the accounts currently asking to follow the caller. Authorize
promotes the pending request into a real Follow (counters move,
notification to the requester). Reject just discards the request row.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select

from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.deps import CurrentAccount, DBSession
from app.python.models import Account, FollowRequest
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.relationship import Relationship, serialize_relationship
from app.python.services import follows as follow_service
from app.python.services.account_relationships import load_account_relationships

router = APIRouter(prefix="/api/v1/follow_requests", tags=["follow_requests"])


@router.get("", response_model=list[Account_])
async def index(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Account_]:
    stmt = apply_pagination(
        select(FollowRequest.id, FollowRequest.account_id).where(
            FollowRequest.target_account_id == viewer.id
        ),
        FollowRequest.id,
        params,
    )
    pairs = (await session.execute(stmt)).all()
    cursors = [_Pair(jid, aid) for jid, aid in pairs]
    ordered = maybe_reverse(cursors, params)
    if not ordered:
        return []

    account_ids = [c.account_id for c in ordered]
    accounts = (
        await session.execute(select(Account).where(Account.id.in_(account_ids)))
    ).unique().scalars().all()
    by_id = {a.id: a for a in accounts}
    rows = [by_id[c.account_id] for c in ordered if c.account_id in by_id]

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
        id_attr="id",
    )
    if link:
        response.headers["Link"] = link

    return [serialize_account(a) for a in rows]


@router.post("/{account_id}/authorize", response_model=Relationship)
async def authorize(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Relationship:
    requester = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if requester is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    result = await follow_service.authorize_follow_request(
        session, target=viewer, requester=requester
    )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/reject", response_model=Relationship)
async def reject(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Relationship:
    requester = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if requester is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    await follow_service.reject_follow_request(
        session, target=viewer, requester=requester
    )
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


class _Pair:
    """Pagination row — see `routers/listings.py` for the contract."""

    __slots__ = ("account_id", "id")

    def __init__(self, jid: int, aid: int) -> None:
        self.id = jid
        self.account_id = aid
