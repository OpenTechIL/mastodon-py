"""`/api/v1/tags/*` and `/api/v1/timelines/tag/{hashtag}`."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select

from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.common.snowflake import now_id
from app.python.deps import CurrentAccount, DBSession, OptionalAuth
from app.python.models import Mute, Status, StatusTag, Tag, TagFollow, Visibility
from app.python.schemas.status import Status_, serialize_status
from app.python.schemas.tag import Tag_, serialize_tag
from app.python.services.status_relationships import (
    load_relationships,
    status_ids_for_batch,
)

router = APIRouter(tags=["tags"])


async def _resolve_tag(session, name: str) -> Tag:
    """Case-insensitive lookup. The schema's unique index is on `lower(name)`
    so `func.lower(Tag.name)` hits the index in production."""
    row = (
        await session.execute(
            select(Tag).where(func.lower(Tag.name) == name.lower()).limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return row


async def _is_following(session, account_id: int, tag_id: int) -> bool:
    return (
        await session.execute(
            select(TagFollow.id)
            .where(TagFollow.account_id == account_id, TagFollow.tag_id == tag_id)
            .limit(1)
        )
    ).scalar_one_or_none() is not None


@router.get("/api/v1/tags/{name}", response_model=Tag_)
async def show(
    name: str,
    session: DBSession,
    auth: OptionalAuth,
) -> Tag_:
    tag = await _resolve_tag(session, name)
    following = (
        await _is_following(session, auth.account.id, tag.id)
        if auth and auth.account
        else False
    )
    return serialize_tag(tag, following=following)


@router.post("/api/v1/tags/{name}/follow", response_model=Tag_)
async def follow_tag(
    name: str,
    session: DBSession,
    viewer: CurrentAccount,
) -> Tag_:
    tag = await _resolve_tag(session, name)
    existing = (
        await session.execute(
            select(TagFollow).where(
                TagFollow.account_id == viewer.id, TagFollow.tag_id == tag.id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        session.add(
            TagFollow(
                id=now_id(),
                account_id=viewer.id,
                tag_id=tag.id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    return serialize_tag(tag, following=True)


@router.post("/api/v1/tags/{name}/unfollow", response_model=Tag_)
async def unfollow_tag(
    name: str,
    session: DBSession,
    viewer: CurrentAccount,
) -> Tag_:
    tag = await _resolve_tag(session, name)
    from sqlalchemy import delete

    await session.execute(
        delete(TagFollow).where(
            TagFollow.account_id == viewer.id, TagFollow.tag_id == tag.id
        )
    )
    await session.commit()
    return serialize_tag(tag, following=False)


@router.get("/api/v1/timelines/tag/{hashtag}", response_model=list[Status_])
async def tag_timeline(
    hashtag: str,
    request: Request,
    response: Response,
    session: DBSession,
    auth: OptionalAuth,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Status_]:
    tag = (
        await session.execute(
            select(Tag).where(func.lower(Tag.name) == hashtag.lower()).limit(1)
        )
    ).scalar_one_or_none()
    if tag is None:
        return []

    viewer_account_id = auth.account.id if (auth and auth.account) else None
    stmt = (
        select(Status)
        .join(StatusTag, StatusTag.status_id == Status.id)
        .where(
            StatusTag.tag_id == tag.id,
            Status.deleted_at.is_(None),
            Status.visibility.in_([Visibility.PUBLIC.value, Visibility.UNLISTED.value]),
        )
    )
    if viewer_account_id is not None:
        muted = select(Mute.target_account_id).where(Mute.account_id == viewer_account_id)
        stmt = stmt.where(Status.account_id.not_in(muted))

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
        session, viewer_account_id, status_ids_for_batch(ordered)
    )
    return [serialize_status(s, relationships=relationships) for s in ordered]
