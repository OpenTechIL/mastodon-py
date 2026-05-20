"""`/api/v2/filters` — CRUD for content filters + nested keywords/statuses.

Filters are scoped to the calling account; admin-side override does not
apply.  Action and context are validated against `VALID_CONTEXTS` and
the `FilterAction` enum so a typo on POST fails fast instead of
silently writing a filter that never matches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from app.python.common.snowflake import now_id
from app.python.deps import CurrentAccount, DBSession
from app.python.models import (
    VALID_CONTEXTS,
    CustomFilter,
    CustomFilterKeyword,
    CustomFilterStatus,
    FilterAction,
    parse_filter_action,
)

router = APIRouter(prefix="/api/v2/filters", tags=["filters"])


# ---------- Schemas ----------


class FilterKeyword_(BaseModel):
    id: str
    keyword: str
    whole_word: bool


class FilterStatus_(BaseModel):
    id: str
    status_id: str


class Filter_(BaseModel):
    id: str
    title: str
    context: list[str]
    expires_at: datetime | None
    filter_action: str
    keywords: list[FilterKeyword_] = Field(default_factory=list)
    statuses: list[FilterStatus_] = Field(default_factory=list)


class FilterCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(default="")
    context: list[str] = Field(default_factory=list)
    filter_action: str | None = None
    expires_in: int | None = Field(default=None, ge=0)


class FilterUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    context: list[str] | None = None
    filter_action: str | None = None
    expires_in: int | None = Field(default=None, ge=0)


class KeywordBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keyword: str = Field(default="")
    whole_word: bool = True


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status_id: int


# ---------- Helpers ----------


def _validate_context(ctx: list[str]) -> list[str]:
    bad = [c for c in ctx if c not in VALID_CONTEXTS]
    if bad:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid context values: {bad}",
        )
    return list(ctx)


def _expires_at(now: datetime, expires_in: int | None) -> datetime | None:
    if expires_in is None:
        return None
    if expires_in == 0:
        return None
    from datetime import timedelta

    return now + timedelta(seconds=expires_in)


async def _owned_filter(session, owner_id: int, filter_id: int) -> CustomFilter:
    row = (
        await session.execute(
            select(CustomFilter).where(
                CustomFilter.id == filter_id,
                CustomFilter.account_id == owner_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return row


async def _serialize_filter(session, f: CustomFilter) -> Filter_:
    keywords = (
        (await session.execute(select(CustomFilterKeyword).where(CustomFilterKeyword.custom_filter_id == f.id)))
        .scalars()
        .all()
    )
    statuses = (
        (await session.execute(select(CustomFilterStatus).where(CustomFilterStatus.custom_filter_id == f.id)))
        .scalars()
        .all()
    )
    return Filter_(
        id=str(f.id),
        title=f.phrase,
        context=list(f.context or []),
        expires_at=f.expires_at,
        filter_action=FilterAction(f.action).name_for_api,
        keywords=[FilterKeyword_(id=str(k.id), keyword=k.keyword, whole_word=k.whole_word) for k in keywords],
        statuses=[FilterStatus_(id=str(s.id), status_id=str(s.status_id)) for s in statuses],
    )


# ---------- Filter CRUD ----------


@router.get("", response_model=list[Filter_])
async def index(
    session: DBSession,
    viewer: CurrentAccount,
) -> list[Filter_]:
    rows = (
        (
            await session.execute(
                select(CustomFilter).where(CustomFilter.account_id == viewer.id).order_by(CustomFilter.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [await _serialize_filter(session, f) for f in rows]


@router.post("", response_model=Filter_, status_code=status.HTTP_200_OK)
async def create(
    body: FilterCreate,
    session: DBSession,
    viewer: CurrentAccount,
) -> Filter_:
    try:
        action = parse_filter_action(body.filter_action)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    context = _validate_context(body.context)
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = CustomFilter(
        id=now_id(),
        account_id=viewer.id,
        action=action.value,
        context=context,
        expires_at=_expires_at(now, body.expires_in),
        phrase=body.title,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return await _serialize_filter(session, row)


@router.get("/{filter_id}", response_model=Filter_)
async def show(
    filter_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Filter_:
    row = await _owned_filter(session, viewer.id, filter_id)
    return await _serialize_filter(session, row)


@router.put("/{filter_id}", response_model=Filter_)
async def update(
    filter_id: int,
    body: FilterUpdate,
    session: DBSession,
    viewer: CurrentAccount,
) -> Filter_:
    row = await _owned_filter(session, viewer.id, filter_id)
    if body.title is not None:
        row.phrase = body.title
    if body.context is not None:
        row.context = _validate_context(body.context)
    if body.filter_action is not None:
        try:
            row.action = parse_filter_action(body.filter_action).value
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if body.expires_in is not None:
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        row.expires_at = _expires_at(now, body.expires_in)
    row.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)
    await session.commit()
    return await _serialize_filter(session, row)


@router.delete("/{filter_id}", status_code=status.HTTP_200_OK)
async def destroy(
    filter_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, Any]:
    await _owned_filter(session, viewer.id, filter_id)
    await session.execute(delete(CustomFilterKeyword).where(CustomFilterKeyword.custom_filter_id == filter_id))
    await session.execute(delete(CustomFilterStatus).where(CustomFilterStatus.custom_filter_id == filter_id))
    await session.execute(delete(CustomFilter).where(CustomFilter.id == filter_id))
    await session.commit()
    return {}


# ---------- Keywords sub-resource ----------


@router.get("/{filter_id}/keywords", response_model=list[FilterKeyword_])
async def list_keywords(
    filter_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> list[FilterKeyword_]:
    await _owned_filter(session, viewer.id, filter_id)
    rows = (
        (
            await session.execute(
                select(CustomFilterKeyword)
                .where(CustomFilterKeyword.custom_filter_id == filter_id)
                .order_by(CustomFilterKeyword.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [FilterKeyword_(id=str(k.id), keyword=k.keyword, whole_word=k.whole_word) for k in rows]


@router.post(
    "/{filter_id}/keywords",
    response_model=FilterKeyword_,
    status_code=status.HTTP_200_OK,
)
async def add_keyword(
    filter_id: int,
    body: KeywordBody,
    session: DBSession,
    viewer: CurrentAccount,
) -> FilterKeyword_:
    await _owned_filter(session, viewer.id, filter_id)
    if not body.keyword:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="keyword can't be blank")
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = CustomFilterKeyword(
        id=now_id(),
        custom_filter_id=filter_id,
        keyword=body.keyword,
        whole_word=body.whole_word,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return FilterKeyword_(id=str(row.id), keyword=row.keyword, whole_word=row.whole_word)


@router.delete("/{filter_id}/keywords/{keyword_id}", status_code=status.HTTP_200_OK)
async def remove_keyword(
    filter_id: int,
    keyword_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, Any]:
    await _owned_filter(session, viewer.id, filter_id)
    await session.execute(
        delete(CustomFilterKeyword).where(
            CustomFilterKeyword.custom_filter_id == filter_id,
            CustomFilterKeyword.id == keyword_id,
        )
    )
    await session.commit()
    return {}


# ---------- Statuses sub-resource ----------


@router.get("/{filter_id}/statuses", response_model=list[FilterStatus_])
async def list_statuses(
    filter_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> list[FilterStatus_]:
    await _owned_filter(session, viewer.id, filter_id)
    rows = (
        (
            await session.execute(
                select(CustomFilterStatus)
                .where(CustomFilterStatus.custom_filter_id == filter_id)
                .order_by(CustomFilterStatus.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [FilterStatus_(id=str(s.id), status_id=str(s.status_id)) for s in rows]


@router.post(
    "/{filter_id}/statuses",
    response_model=FilterStatus_,
    status_code=status.HTTP_200_OK,
)
async def add_status(
    filter_id: int,
    body: StatusBody,
    session: DBSession,
    viewer: CurrentAccount,
) -> FilterStatus_:
    await _owned_filter(session, viewer.id, filter_id)
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = CustomFilterStatus(
        id=now_id(),
        custom_filter_id=filter_id,
        status_id=body.status_id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return FilterStatus_(id=str(row.id), status_id=str(row.status_id))


@router.delete("/{filter_id}/statuses/{filter_status_id}", status_code=status.HTTP_200_OK)
async def remove_status(
    filter_id: int,
    filter_status_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, Any]:
    await _owned_filter(session, viewer.id, filter_id)
    await session.execute(
        delete(CustomFilterStatus).where(
            CustomFilterStatus.custom_filter_id == filter_id,
            CustomFilterStatus.id == filter_status_id,
        )
    )
    await session.commit()
    return {}


# ---------- v1 compatibility ----------

v1_router = APIRouter(prefix="/api/v1/filters", tags=["filters"])


class _FilterV1(BaseModel):
    id: str
    phrase: str
    context: list[str]
    expires_at: datetime | None
    filter_action: str
    irreversible: bool
    whole_word: bool


@v1_router.get("", response_model=list[_FilterV1])
async def index_v1(session: DBSession, viewer: CurrentAccount) -> list[_FilterV1]:
    """v1 flat filter list — first keyword becomes the phrase."""
    rows = (
        (
            await session.execute(
                select(CustomFilter).where(CustomFilter.account_id == viewer.id).order_by(CustomFilter.id.asc())
            )
        )
        .scalars()
        .all()
    )
    result = []
    for f in rows:
        kws = (
            (
                await session.execute(
                    select(CustomFilterKeyword).where(CustomFilterKeyword.custom_filter_id == f.id).limit(1)
                )
            )
            .scalars()
            .all()
        )
        phrase = kws[0].keyword if kws else ""
        whole_word = kws[0].whole_word if kws else False
        result.append(
            _FilterV1(
                id=str(f.id),
                phrase=phrase,
                context=f.context or [],
                expires_at=f.expires_at,
                filter_action=str(f.action or "warn"),
                irreversible=str(f.action) == "hide",
                whole_word=whole_word,
            )
        )
    return result
