"""`/api/v1/featured_tags` + `/accounts/{id}/featured_tags`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from app.python.common.snowflake import now_id
from app.python.deps import CurrentAccount, DBSession
from app.python.lib.asset_urls import _asset_host
from app.python.models import Account, FeaturedTag, StatusTag, Tag

router = APIRouter(tags=["featured_tags"])


MAX_FEATURED_TAGS = 10


class FeaturedTag_(BaseModel):
    id: str
    name: str
    url: str
    statuses_count: str
    last_status_at: str | None


class FeaturedTagCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="")


def _serialize(ft: FeaturedTag, tag: Tag, owner: Account) -> FeaturedTag_:
    return FeaturedTag_(
        id=str(ft.id),
        name=ft.name or tag.display_name or tag.name,
        url=f"{_asset_host()}/@{owner.username}/tagged/{tag.name}",
        statuses_count=str(ft.statuses_count),
        last_status_at=ft.last_status_at.date().isoformat() if ft.last_status_at else None,
    )


async def _load_owner(session, account_id: int) -> Account:
    row = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if row is None or row.suspended:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return row


async def _list_for(session, owner: Account) -> list[FeaturedTag_]:
    rows = (
        await session.execute(
            select(FeaturedTag, Tag)
            .join(Tag, Tag.id == FeaturedTag.tag_id)
            .where(FeaturedTag.account_id == owner.id)
            .order_by(FeaturedTag.id.asc())
        )
    ).all()
    return [_serialize(ft, tag, owner) for ft, tag in rows]


@router.get("/api/v1/featured_tags", response_model=list[FeaturedTag_])
async def index(
    session: DBSession,
    viewer: CurrentAccount,
) -> list[FeaturedTag_]:
    return await _list_for(session, viewer)


@router.post(
    "/api/v1/featured_tags",
    response_model=FeaturedTag_,
    status_code=status.HTTP_200_OK,
)
async def create(
    body: FeaturedTagCreate,
    session: DBSession,
    viewer: CurrentAccount,
) -> FeaturedTag_:
    name = body.name.strip().lstrip("#").lower()
    if not name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="name can't be blank"
        )

    count = (
        await session.execute(
            select(func.count()).select_from(FeaturedTag).where(
                FeaturedTag.account_id == viewer.id
            )
        )
    ).scalar_one()
    if count >= MAX_FEATURED_TAGS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"You can only feature up to {MAX_FEATURED_TAGS} tags",
        )

    # Tag must already exist — Mastodon requires you've used a tag at
    # least once before featuring it. If it doesn't exist, 422.
    tag = (
        await session.execute(
            select(Tag).where(func.lower(Tag.name) == name).limit(1)
        )
    ).scalar_one_or_none()
    if tag is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="You must have used this tag at least once",
        )

    # Idempotent: return the existing row if already featured.
    existing = (
        await session.execute(
            select(FeaturedTag).where(
                FeaturedTag.account_id == viewer.id,
                FeaturedTag.tag_id == tag.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _serialize(existing, tag, viewer)

    # Compute the denormalised counts from the join table. (statuses_count
    # walks the viewer's tagged statuses.)
    from app.python.models import Status

    counts_row = (
        await session.execute(
            select(
                func.count(Status.id), func.max(Status.created_at)
            )
            .select_from(Status)
            .join(StatusTag, StatusTag.status_id == Status.id)
            .where(
                StatusTag.tag_id == tag.id,
                Status.account_id == viewer.id,
                Status.deleted_at.is_(None),
            )
        )
    ).one()
    statuses_count = int(counts_row[0] or 0)
    last_status_at = counts_row[1]

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = FeaturedTag(
        id=now_id(),
        account_id=viewer.id,
        tag_id=tag.id,
        name=tag.display_name or tag.name,
        statuses_count=statuses_count,
        last_status_at=last_status_at,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return _serialize(row, tag, viewer)


@router.delete("/api/v1/featured_tags/{featured_tag_id}", status_code=status.HTTP_200_OK)
async def destroy(
    featured_tag_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(FeaturedTag).where(
                FeaturedTag.id == featured_tag_id,
                FeaturedTag.account_id == viewer.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    await session.execute(delete(FeaturedTag).where(FeaturedTag.id == featured_tag_id))
    await session.commit()
    return {}


@router.get(
    "/api/v1/accounts/{account_id}/featured_tags",
    response_model=list[FeaturedTag_],
)
async def index_for_account(
    account_id: int,
    session: DBSession,
) -> list[FeaturedTag_]:
    owner = await _load_owner(session, account_id)
    return await _list_for(session, owner)



@router.get("/api/v1/featured_tags/suggestions")
async def featured_tag_suggestions(
    session: DBSession,
    account: CurrentAccount,
) -> list[Any]:
    """Return hashtags the current user has used recently as suggestions
    for featured tags. Returns top 10 by recent usage."""
    from app.python.models import Status, StatusTag, Tag

    rows = (
        await session.execute(
            select(Tag.name, func.count(StatusTag.tag_id).label("cnt"))
            .join(StatusTag, StatusTag.tag_id == Tag.id)
            .join(Status, Status.id == StatusTag.status_id)
            .where(Status.account_id == account.id, Status.deleted_at.is_(None))
            .group_by(Tag.id, Tag.name)
            .order_by(func.count(StatusTag.tag_id).desc())
            .limit(10)
        )
    ).all()

    return [{"name": row.name} for row in rows]
