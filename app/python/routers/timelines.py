"""`/api/v1/timelines/*` endpoints — public timeline only for this slice."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import or_, select

from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.deps import CurrentAccount, DBSession, OptionalAuth
from app.python.models import (
    Account,
    AccountDomainBlock,
    Block,
    Follow,
    ListAccount,
    Mute,
    Status,
    StatusTag,
    Tag,
    Visibility,
)
from app.python.models import (
    List as List_,
)
from app.python.schemas.status import Status_, serialize_status
from app.python.services.filter_application import load_filters_for
from app.python.services.status_relationships import (
    load_relationships,
    status_ids_for_batch,
)

router = APIRouter(prefix="/api/v1/timelines", tags=["timelines"])


@router.get("/public", response_model=list[Status_])
async def public_timeline(
    request: Request,
    response: Response,
    session: DBSession,
    auth: OptionalAuth,
    params: Annotated[PageParams, Depends(page_params)],
    local: bool = Query(default=False),
    remote: bool = Query(default=False),
) -> list[Status_]:
    """Mirror of `Api::V1::Timelines::PublicController#index`.

    Filters: visibility=public, not a reblog, not a reply (unless self-reply),
    not soft-deleted. `?local` restricts to local-origin posts; `?remote` to
    remote-origin. Both flags omitted = federated firehose (legacy
    "public" behavior).
    """
    stmt = select(Status).where(
        Status.visibility == Visibility.PUBLIC.value,
        Status.reblog_of_id.is_(None),
        Status.deleted_at.is_(None),
        (Status.reply.is_(False)) | (Status.in_reply_to_account_id == Status.account_id),
    )

    if local:
        stmt = stmt.where((Status.local.is_(True)) | (Status.uri.is_(None)))
    elif remote:
        stmt = stmt.where(Status.local.is_(False))

    stmt = apply_pagination(stmt, Status.id, params)
    rows = (await session.execute(stmt)).unique().scalars().all()
    ordered = maybe_reverse(rows, params)

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
        extra_query=_extra_for_link(local=local, remote=remote),
    )
    if link:
        response.headers["Link"] = link

    viewer_account_id = auth.account.id if (auth and auth.account) else None
    relationships = await load_relationships(
        session, viewer_account_id, status_ids_for_batch(ordered)
    )
    filter_checks = await load_filters_for(session, viewer_account_id, "public")
    return [
        serialize_status(row, relationships=relationships, filter_checks=filter_checks)
        for row in ordered
    ]


def _extra_for_link(*, local: bool, remote: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    if local:
        out["local"] = "true"
    if remote:
        out["remote"] = "true"
    return out


@router.get("/home", response_model=list[Status_])
async def home_timeline(
    request: Request,
    response: Response,
    session: DBSession,
    account: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Status_]:
    """Statuses authored by the viewer or by accounts the viewer follows.

    Direct SQL fan-in. The legacy backend serves home from a Redis ZSET
    pre-populated by `FanOutOnWriteService`; that index lands when the
    Redis fan-out phase ports. Until then this query reads the same
    source-of-truth statuses Redis is indexing.
    """
    followee_ids = select(Follow.target_account_id).where(Follow.account_id == account.id)
    muted_ids = select(Mute.target_account_id).where(Mute.account_id == account.id)
    blocking_ids = select(Block.target_account_id).where(Block.account_id == account.id)
    blocked_by_ids = select(Block.account_id).where(Block.target_account_id == account.id)
    # Accounts whose domain is on the viewer's domain-block list.
    blocked_domain_accounts = (
        select(Account.id)
        .join(
            AccountDomainBlock,
            AccountDomainBlock.domain == Account.domain,
        )
        .where(AccountDomainBlock.account_id == account.id)
    )

    stmt = select(Status).where(
        Status.deleted_at.is_(None),
        or_(
            Status.account_id == account.id,
            Status.account_id.in_(followee_ids),
        ),
        Status.account_id.not_in(muted_ids),
        Status.account_id.not_in(blocking_ids),
        Status.account_id.not_in(blocked_by_ids),
        Status.account_id.not_in(blocked_domain_accounts),
    )
    stmt = apply_pagination(stmt, Status.id, params)
    rows = (await session.execute(stmt)).unique().scalars().all()
    ordered = maybe_reverse(rows, params)

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
    )
    if link:
        response.headers["Link"] = link

    relationships = await load_relationships(
        session, account.id, status_ids_for_batch(ordered)
    )
    filter_checks = await load_filters_for(session, account.id, "home")
    return [
        serialize_status(row, relationships=relationships, filter_checks=filter_checks)
        for row in ordered
    ]


@router.get("/tag/{hashtag}", response_model=list[Status_])
async def tag_timeline(
    hashtag: str,
    request: Request,
    response: Response,
    session: DBSession,
    auth: OptionalAuth,
    params: Annotated[PageParams, Depends(page_params)],
    local: bool = Query(False),
    remote: bool = Query(False),
    only_media: bool = Query(False),
) -> list[Status_]:
    tag_q = select(Tag).where(Tag.name == hashtag.lower())
    tag = (await session.execute(tag_q)).scalars().first()
    if not tag:
        return []
    stmt = (
        select(Status)
        .join(StatusTag, StatusTag.status_id == Status.id)
        .where(
            StatusTag.tag_id == tag.id,
            Status.deleted_at.is_(None),
            Status.visibility.in_([Visibility.PUBLIC.value, Visibility.UNLISTED.value]),
        )
    )
    if local:
        stmt = stmt.where(Status.local.is_(True))
    if remote:
        stmt = stmt.where(Status.local.is_(False))
    stmt = apply_pagination(stmt, Status.id, params)
    rows = (await session.execute(stmt)).unique().scalars().all()
    ordered = maybe_reverse(rows, params)
    if ordered:
        response.headers["Link"] = build_link_header(
            str(request.url.include_query_params().replace(query="")), ordered, params,
        ) or ""
    viewer_id = auth.account.id if auth and auth.account else None
    relationships = await load_relationships(session, viewer_id, status_ids_for_batch(ordered))
    return [serialize_status(row, relationships=relationships) for row in ordered]


@router.get("/list/{list_id}", response_model=list[Status_])
async def list_timeline(
    list_id: int,
    request: Request,
    response: Response,
    session: DBSession,
    account: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Status_]:
    from fastapi import HTTPException
    list_q = select(List_).where(List_.id == list_id, List_.account_id == account.id)
    lst = (await session.execute(list_q)).scalars().first()
    if not lst:
        raise HTTPException(status_code=404, detail="Record not found")
    member_ids = select(ListAccount.account_id).where(ListAccount.list_id == list_id)
    stmt = (
        select(Status)
        .where(
            Status.account_id.in_(member_ids),
            Status.deleted_at.is_(None),
        )
    )
    stmt = apply_pagination(stmt, Status.id, params)
    rows = (await session.execute(stmt)).unique().scalars().all()
    ordered = maybe_reverse(rows, params)
    if ordered:
        response.headers["Link"] = build_link_header(
            str(request.url.include_query_params().replace(query="")), ordered, params,
        ) or ""
    relationships = await load_relationships(session, account.id, status_ids_for_batch(ordered))
    return [serialize_status(row, relationships=relationships) for row in ordered]


@router.get("/link", response_model=list[Status_])
async def link_timeline(
    url: str = Query(...),
) -> list[Status_]:
    """Statuses that link to a given URL (trending link timeline)."""
    return []
