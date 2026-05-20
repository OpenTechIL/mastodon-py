"""`/api/v2/search` — multi-type substring search.

Three result types in one response:

  - **accounts**: ILIKE on username + display_name.
  - **statuses**: ILIKE on text + spoiler_text, visibility-scoped to
    the viewer (public+unlisted for anon; +mentioned/follower-private
    via the existing visibility policy on each result).
  - **hashtags**: ILIKE on `tags.name`.

`?type=` narrows to one category. `?resolve=true` (webfinger fetch for
unknown remote acct) is accepted but ignored until federation ports.
Pagination via `limit`/`offset` is allowed for authenticated callers
only — matches Mastodon's anti-scrape gate.

Elasticsearch is the legacy backend's storage; this slice goes
direct-to-Postgres (no Chewy index). Performance is acceptable for
single-instance dev; Phase 5 swaps the backend.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.python.deps import DBSession, OptionalAuth
from app.python.models import Account, Status, Tag, Visibility
from app.python.policies.status_policy import visible_to
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.status import Status_, serialize_status
from app.python.schemas.tag import Tag_, serialize_tag
from app.python.services.status_relationships import (
    load_relationships,
    status_ids_for_batch,
)

router = APIRouter(tags=["search"])


class SearchResults(BaseModel):
    accounts: list[Account_] = Field(default_factory=list)
    statuses: list[Status_] = Field(default_factory=list)
    hashtags: list[Tag_] = Field(default_factory=list)


SearchType = Literal["accounts", "statuses", "hashtags"]


@router.get("/api/v1/search", response_model=SearchResults)
@router.get("/api/v2/search", response_model=SearchResults)
async def search(
    session: DBSession,
    auth: OptionalAuth,
    q: str = Query(...),
    type: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=40),
    offset: int = Query(default=0, ge=0),
    following: bool = Query(default=False),
    resolve: bool = Query(default=False),
) -> SearchResults:
    q = q.strip()
    if not q:
        return SearchResults()

    is_authed = auth is not None and auth.account is not None
    if offset > 0 and not is_authed:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Search queries pagination is not supported without authentication",
        )

    out = SearchResults()
    needle = f"%{q.lstrip('@#').strip()}%"
    viewer_account_id = auth.account.id if is_authed else None

    if type in (None, "accounts"):
        out.accounts = await _search_accounts(
            session, needle, limit, offset, following=following, viewer=auth
        )
    if type in (None, "hashtags"):
        out.hashtags = await _search_hashtags(
            session, needle, limit, offset, viewer_account_id=viewer_account_id
        )
    if type in (None, "statuses"):
        out.statuses = await _search_statuses(
            session, needle, limit, offset, viewer_account_id=viewer_account_id
        )
    return out


async def _search_accounts(
    session, needle: str, limit: int, offset: int, *, following: bool, viewer
) -> list[Account_]:
    stmt = (
        select(Account)
        .where(
            or_(
                Account.username.ilike(needle),
                Account.display_name.ilike(needle),
            ),
            Account.suspended_at.is_(None),
        )
        .order_by(Account.id.asc())
        .offset(offset)
        .limit(limit)
    )
    if following and viewer and viewer.account:
        from app.python.models import Follow

        followee_ids = select(Follow.target_account_id).where(
            Follow.account_id == viewer.account.id
        )
        stmt = stmt.where(Account.id.in_(followee_ids))
    rows = (await session.execute(stmt)).unique().scalars().all()
    return [serialize_account(a) for a in rows]


async def _search_hashtags(
    session,
    needle: str,
    limit: int,
    offset: int,
    *,
    viewer_account_id: int | None,
) -> list[Tag_]:
    from app.python.models import TagFollow

    stmt = (
        select(Tag)
        .where(Tag.name.ilike(needle))
        .order_by(Tag.id.asc())
        .offset(offset)
        .limit(limit)
    )
    tags = (await session.execute(stmt)).unique().scalars().all()
    if not tags:
        return []

    following_ids: set[int] = set()
    if viewer_account_id is not None:
        followed = (
            await session.execute(
                select(TagFollow.tag_id).where(
                    TagFollow.account_id == viewer_account_id,
                    TagFollow.tag_id.in_([t.id for t in tags]),
                )
            )
        ).scalars().all()
        following_ids = set(followed)
    return [serialize_tag(t, following=t.id in following_ids) for t in tags]


async def _search_statuses(
    session,
    needle: str,
    limit: int,
    offset: int,
    *,
    viewer_account_id: int | None,
) -> list[Status_]:
    # SQL pre-filter narrows the candidate set to public+unlisted-or-author;
    # the policy check below handles private/direct correctly per status.
    base_filter = [
        or_(
            Status.text.ilike(needle),
            Status.spoiler_text.ilike(needle),
        ),
        Status.deleted_at.is_(None),
    ]
    if viewer_account_id is None:
        base_filter.append(
            Status.visibility.in_(
                [Visibility.PUBLIC.value, Visibility.UNLISTED.value]
            )
        )
    stmt = (
        select(Status)
        .where(*base_filter)
        .order_by(Status.id.desc())
        .offset(offset)
        .limit(limit * 2)  # overfetch so visibility-policy filtering still hits `limit`
    )
    candidates = (await session.execute(stmt)).unique().scalars().all()

    visible: list[Status] = []
    for s in candidates:
        if await visible_to(session, s, viewer_account_id):
            visible.append(s)
            if len(visible) >= limit:
                break

    relationships = await load_relationships(
        session, viewer_account_id, status_ids_for_batch(visible)
    )
    return [serialize_status(s, relationships=relationships) for s in visible]
