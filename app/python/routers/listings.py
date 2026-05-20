"""Listing endpoints for the caller's own join-table memberships.

These all share the same shape:

  - Cursor is the join row's `id` (e.g. `Favourite.id`).
  - Response body is the related entity (Status, Account).
  - Visibility / suspended filtering applies to the rendered entity, not
    the cursor — a status that's since been deleted still consumes a
    cursor slot but is filtered out of the response.

Mastodon clients page these via the `Link` header's `max_id`/`min_id`
attached to the join row, NOT the entity. That's the only subtlety the
shared helpers below have to honor.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.deps import CurrentAccount, DBSession
from app.python.models import (
    Account,
    AccountPin,
    Block,
    Bookmark,
    Favourite,
    Mute,
    Status,
)
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.status import Status_, serialize_status
from app.python.services.status_relationships import (
    load_relationships,
    status_ids_for_batch,
)

router = APIRouter(tags=["listings"])


@dataclass(slots=True)
class _Cursor:
    """Pagination row carrying the join `id` (the cursor) and the related
    entity's id (what we hydrate)."""

    id: int  # the join row's id — what the Link header advertises
    entity_id: int


def _to_cursors(pairs: Iterable[tuple[int, int]]) -> list[_Cursor]:
    return [_Cursor(id=jid, entity_id=eid) for jid, eid in pairs]


# ---------- /api/v1/favourites ----------


@router.get("/api/v1/favourites", response_model=list[Status_])
async def favourites_listing(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Status_]:
    stmt = apply_pagination(
        select(Favourite.id, Favourite.status_id).where(
            Favourite.account_id == viewer.id
        ),
        Favourite.id,
        params,
    )
    cursors = _to_cursors((await session.execute(stmt)).all())
    ordered = maybe_reverse(cursors, params)
    if not ordered:
        return []

    status_ids = [c.entity_id for c in ordered]
    statuses = (
        await session.execute(select(Status).where(Status.id.in_(status_ids)))
    ).unique().scalars().all()
    by_id = {s.id: s for s in statuses if not s.discarded}
    rows = [by_id[c.entity_id] for c in ordered if c.entity_id in by_id]

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
        id_attr="id",
    )
    if link:
        response.headers["Link"] = link

    relationships = await load_relationships(
        session, viewer.id, status_ids_for_batch(rows)
    )
    return [serialize_status(s, relationships=relationships) for s in rows]


# ---------- /api/v1/bookmarks ----------


@router.get("/api/v1/bookmarks", response_model=list[Status_])
async def bookmarks_listing(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Status_]:
    stmt = apply_pagination(
        select(Bookmark.id, Bookmark.status_id).where(
            Bookmark.account_id == viewer.id
        ),
        Bookmark.id,
        params,
    )
    cursors = _to_cursors((await session.execute(stmt)).all())
    ordered = maybe_reverse(cursors, params)
    if not ordered:
        return []

    status_ids = [c.entity_id for c in ordered]
    statuses = (
        await session.execute(select(Status).where(Status.id.in_(status_ids)))
    ).unique().scalars().all()
    by_id = {s.id: s for s in statuses if not s.discarded}
    rows = [by_id[c.entity_id] for c in ordered if c.entity_id in by_id]

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
        id_attr="id",
    )
    if link:
        response.headers["Link"] = link

    relationships = await load_relationships(
        session, viewer.id, status_ids_for_batch(rows)
    )
    return [serialize_status(s, relationships=relationships) for s in rows]


# ---------- /api/v1/blocks ----------


async def _account_listing(
    request: Request,
    response: Response,
    session,
    params: PageParams,
    *,
    join_id_col,
    target_id_col,
    where,  # the per-table filter: e.g. `Block.account_id == viewer.id`
) -> list[Account_]:
    stmt = apply_pagination(
        select(join_id_col, target_id_col).where(where),
        join_id_col,
        params,
    )
    cursors = _to_cursors((await session.execute(stmt)).all())
    ordered = maybe_reverse(cursors, params)
    if not ordered:
        return []

    account_ids = [c.entity_id for c in ordered]
    accounts = (
        await session.execute(select(Account).where(Account.id.in_(account_ids)))
    ).unique().scalars().all()
    by_id = {a.id: a for a in accounts}
    rows = [by_id[c.entity_id] for c in ordered if c.entity_id in by_id]

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
        id_attr="id",
    )
    if link:
        response.headers["Link"] = link

    return [serialize_account(a) for a in rows]


@router.get("/api/v1/blocks", response_model=list[Account_])
async def blocks_listing(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Account_]:
    return await _account_listing(
        request,
        response,
        session,
        params,
        join_id_col=Block.id,
        target_id_col=Block.target_account_id,
        where=Block.account_id == viewer.id,
    )


@router.get("/api/v1/endorsements", response_model=list[Account_])
async def endorsements_listing(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Account_]:
    return await _account_listing(
        request,
        response,
        session,
        params,
        join_id_col=AccountPin.id,
        target_id_col=AccountPin.target_account_id,
        where=AccountPin.account_id == viewer.id,
    )


@router.get("/api/v1/mutes", response_model=list[Account_])
async def mutes_listing(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Account_]:
    return await _account_listing(
        request,
        response,
        session,
        params,
        join_id_col=Mute.id,
        target_id_col=Mute.target_account_id,
        where=Mute.account_id == viewer.id,
    )


# ---------- /api/v1/scheduled_statuses ----------
# Scheduled posting is not yet implemented; return empty list so the
# Mastodon SPA doesn't break when polling this endpoint on load.

@router.get("/api/v1/scheduled_statuses", response_model=list)
async def scheduled_statuses_index(viewer: CurrentAccount) -> list:
    return []


@router.get("/api/v1/scheduled_statuses/{scheduled_status_id}", response_model=dict)
async def scheduled_status_show(
    scheduled_status_id: int,
    viewer: CurrentAccount,
) -> dict:
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Record not found")


@router.put("/api/v1/scheduled_statuses/{scheduled_status_id}", response_model=dict)
async def scheduled_status_update(
    scheduled_status_id: int,
    viewer: CurrentAccount,
) -> dict:
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Record not found")


@router.delete("/api/v1/scheduled_statuses/{scheduled_status_id}", status_code=200)
async def scheduled_status_destroy(
    scheduled_status_id: int,
    viewer: CurrentAccount,
) -> dict:
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Record not found")
