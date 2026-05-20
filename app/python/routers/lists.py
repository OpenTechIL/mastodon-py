"""`/api/v1/lists` endpoints + `/api/v1/timelines/list/{id}`."""

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
from app.python.models import ListAccount, Mute, Status
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.list import (
    List_,
    ListCreate,
    ListMembership,
    ListUpdate,
    serialize_list,
)
from app.python.schemas.status import Status_, serialize_status
from app.python.services import lists as list_service
from app.python.services.status_relationships import (
    load_relationships,
    status_ids_for_batch,
)

router = APIRouter(tags=["lists"])


@router.get("/api/v1/lists", response_model=list[List_])
async def index(
    session: DBSession,
    viewer: CurrentAccount,
) -> list[List_]:
    rows = await list_service.list_lists(session, viewer)
    return [serialize_list(r) for r in rows]


@router.post("/api/v1/lists", response_model=List_, status_code=status.HTTP_200_OK)
async def create(
    body: ListCreate,
    session: DBSession,
    viewer: CurrentAccount,
) -> List_:
    try:
        row = await list_service.create_list(
            session,
            owner=viewer,
            title=body.title,
            replies_policy=body.replies_policy,
            exclusive=body.exclusive,
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return serialize_list(row)


@router.get("/api/v1/lists/{list_id}", response_model=List_)
async def show(
    list_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> List_:
    try:
        row = await list_service._list_for(session, viewer, list_id)
    except list_service.ListNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    return serialize_list(row)


@router.put("/api/v1/lists/{list_id}", response_model=List_)
async def update(
    list_id: int,
    body: ListUpdate,
    session: DBSession,
    viewer: CurrentAccount,
) -> List_:
    try:
        row = await list_service.update_list(
            session,
            owner=viewer,
            list_id=list_id,
            title=body.title,
            replies_policy=body.replies_policy,
            exclusive=body.exclusive,
        )
    except list_service.ListNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return serialize_list(row)


@router.delete("/api/v1/lists/{list_id}", status_code=status.HTTP_200_OK)
async def destroy(
    list_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, object]:
    try:
        await list_service.delete_list(session, owner=viewer, list_id=list_id)
    except list_service.ListNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    return {}


@router.get("/api/v1/lists/{list_id}/accounts", response_model=list[Account_])
async def members(
    list_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> list[Account_]:
    try:
        accounts = await list_service.list_members(
            session, owner=viewer, list_id=list_id
        )
    except list_service.ListNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    return [serialize_account(a) for a in accounts]


@router.post("/api/v1/lists/{list_id}/accounts", status_code=status.HTTP_200_OK)
async def add_members(
    list_id: int,
    body: ListMembership,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, object]:
    try:
        await list_service.add_accounts(
            session, owner=viewer, list_id=list_id, account_ids=body.account_ids
        )
    except list_service.ListNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    return {}


@router.delete("/api/v1/lists/{list_id}/accounts", status_code=status.HTTP_200_OK)
async def remove_members(
    list_id: int,
    body: ListMembership,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, object]:
    try:
        await list_service.remove_accounts(
            session, owner=viewer, list_id=list_id, account_ids=body.account_ids
        )
    except list_service.ListNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    return {}


@router.get("/api/v1/timelines/list/{list_id}", response_model=list[Status_])
async def list_timeline(
    list_id: int,
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Status_]:
    try:
        await list_service._list_for(session, viewer, list_id)
    except list_service.ListNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc

    member_ids = select(ListAccount.account_id).where(ListAccount.list_id == list_id)
    muted_ids = select(Mute.target_account_id).where(Mute.account_id == viewer.id)

    stmt = select(Status).where(
        Status.deleted_at.is_(None),
        Status.account_id.in_(member_ids),
        Status.account_id.not_in(muted_ids),
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
        session, viewer.id, status_ids_for_batch(ordered)
    )
    return [serialize_status(row, relationships=relationships) for row in ordered]
