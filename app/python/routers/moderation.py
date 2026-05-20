"""User-side moderation endpoints.

  - `POST /api/v1/reports` — file a moderation report.
  - `GET / POST / DELETE /api/v1/domain_blocks` — per-account domain blocks.
  - `GET /api/v1/suggestions` — currently an empty stub; the recommender
    pipeline lands with the trending phase.

Admin-side endpoints (`/api/v1/admin/reports/{id}/{resolve,reopen,…}`)
are a separate phase. This module covers what an end user can do.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.common.snowflake import now_id
from app.python.deps import CurrentAccount, DBSession
from app.python.models import (
    Account,
    AccountDomainBlock,
    Follow,
    Report,
    ReportCategory,
    parse_report_category,
)
from app.python.schemas.account import Account_, serialize_account

router = APIRouter(tags=["moderation"])


# ---------- /api/v1/reports ----------


class ReportBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_id: int
    status_ids: list[int] = Field(default_factory=list)
    comment: str = Field(default="", max_length=1000)
    forward: bool = False
    category: str | None = None
    rule_ids: list[int] | None = None


class Report_(BaseModel):
    id: str
    action_taken: bool
    action_taken_at: datetime | None
    category: str
    comment: str
    forwarded: bool | None
    created_at: datetime
    status_ids: list[str]
    rule_ids: list[str]
    target_account: Account_


def _serialize_report(report: Report, target: Account) -> Report_:
    return Report_(
        id=str(report.id),
        action_taken=report.action_taken_at is not None,
        action_taken_at=report.action_taken_at,
        category=ReportCategory(report.category).name_for_api,
        comment=report.comment,
        forwarded=report.forwarded,
        created_at=report.created_at,
        status_ids=[str(s) for s in (report.status_ids or [])],
        rule_ids=[str(r) for r in (report.rule_ids or [])],
        target_account=serialize_account(target),
    )


@router.post("/api/v1/reports", response_model=Report_, status_code=status.HTTP_200_OK)
async def create_report(
    body: ReportBody,
    session: DBSession,
    viewer: CurrentAccount,
) -> Report_:
    if body.account_id == viewer.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="You can't report yourself")
    target = (await session.execute(select(Account).where(Account.id == body.account_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    try:
        category = parse_report_category(body.category)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    row = Report(
        id=now_id(),
        account_id=viewer.id,
        target_account_id=target.id,
        comment=body.comment,
        category=category.value,
        status_ids=list(body.status_ids),
        rule_ids=list(body.rule_ids) if body.rule_ids else None,
        forwarded=body.forward,
        uri=None,
        application_id=None,
        assigned_account_id=None,
        action_taken_at=None,
        action_taken_by_account_id=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return _serialize_report(row, target)


# ---------- /api/v1/domain_blocks ----------


@router.get("/api/v1/domain_blocks/preview")
async def domain_block_preview(
    session: DBSession,
    viewer: CurrentAccount,
    domain: str = Query(default=""),
) -> dict[str, int]:
    """Return follower/following counts that would be lost by blocking a domain."""
    if not domain:
        return {"followers_count": 0, "following_count": 0}
    domain = domain.strip().lower()
    remote_accts = select(Account.id).where(
        Account.domain.ilike(domain),
        Account.domain.is_not(None),
    )
    followers_count = (
        await session.execute(
            select(func.count())
            .select_from(Follow)
            .where(
                Follow.target_account_id == viewer.id,
                Follow.account_id.in_(remote_accts),
            )
        )
    ).scalar() or 0
    following_count = (
        await session.execute(
            select(func.count())
            .select_from(Follow)
            .where(
                Follow.account_id == viewer.id,
                Follow.target_account_id.in_(remote_accts),
            )
        )
    ).scalar() or 0
    return {"followers_count": int(followers_count), "following_count": int(following_count)}


@router.get("/api/v1/domain_blocks", response_model=list[str])
async def domain_blocks_index(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[str]:
    stmt = apply_pagination(
        select(AccountDomainBlock.id, AccountDomainBlock.domain).where(AccountDomainBlock.account_id == viewer.id),
        AccountDomainBlock.id,
        params,
    )
    pairs = (await session.execute(stmt)).all()
    cursors = [_DomainCursor(jid, dom) for jid, dom in pairs]
    ordered = maybe_reverse(cursors, params)

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
        id_attr="id",
    )
    if link:
        response.headers["Link"] = link
    return [c.domain for c in ordered]


class _DomainCursor:
    __slots__ = ("domain", "id")

    def __init__(self, jid: int, domain: str) -> None:
        self.id = jid
        self.domain = domain


class DomainBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    domain: str = Field(..., min_length=1)


@router.post("/api/v1/domain_blocks", status_code=status.HTTP_200_OK)
async def domain_block_add(
    session: DBSession,
    viewer: CurrentAccount,
    domain: str = Query(default=""),
) -> dict[str, Any]:
    """Mastodon historically accepted `domain` as either a query param OR
    a form body field. The React composer uses query params."""
    name = domain.strip().lower()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="domain can't be blank")

    existing = (
        await session.execute(
            select(AccountDomainBlock).where(
                AccountDomainBlock.account_id == viewer.id,
                AccountDomainBlock.domain == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {}
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    session.add(
        AccountDomainBlock(
            id=now_id(),
            account_id=viewer.id,
            domain=name,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()
    return {}


@router.delete("/api/v1/domain_blocks", status_code=status.HTTP_200_OK)
async def domain_block_remove(
    session: DBSession,
    viewer: CurrentAccount,
    domain: str = Query(default=""),
) -> dict[str, Any]:
    name = domain.strip().lower()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="domain can't be blank")
    await session.execute(
        delete(AccountDomainBlock).where(
            AccountDomainBlock.account_id == viewer.id,
            AccountDomainBlock.domain == name,
        )
    )
    await session.commit()
    return {}


# ---------- /api/v1/suggestions ----------


@router.get("/api/v1/suggestions", response_model=list[dict[str, Any]])
async def suggestions_index() -> list[dict[str, Any]]:
    """Stub. Real follow-recommendation requires the trending/recommender
    pipeline + the `account_summaries` materialized view (Phase 5)."""
    return []


@router.get("/api/v2/suggestions", response_model=list[dict[str, Any]])
async def suggestions_index_v2() -> list[dict[str, Any]]:
    return []


@router.delete("/api/v1/suggestions/{account_id}", status_code=200)
async def dismiss_suggestion(account_id: int) -> dict:
    return {}


@router.delete("/api/v2/suggestions/{account_id}", status_code=200)
async def dismiss_suggestion_v2(account_id: int) -> dict:
    return {}


# ---------- /api/v1/emails ----------


@router.get("/api/v1/emails/check_confirmation", response_model=dict)
async def check_email_confirmation() -> dict:
    """Stub — email confirmation flow not yet ported."""
    return {"confirmed": True}


@router.post("/api/v1/emails/confirmations", status_code=200)
async def request_email_confirmation() -> dict:
    return {}


# ---------- /api/v1/donation_campaigns ----------


@router.get("/api/v1/donation_campaigns", response_model=list)
async def donation_campaigns() -> list:
    return []
