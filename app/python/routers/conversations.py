"""`/api/v1/conversations` + `/api/v1/timelines/direct`."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, or_, select

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
    AccountConversation,
    Mention,
    Status,
    Visibility,
)
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.status import Status_, serialize_status
from app.python.services.status_relationships import (
    load_relationships,
    status_ids_for_batch,
)

router = APIRouter(tags=["conversations"])


# ---------- /api/v1/conversations ----------


class Conversation_(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    unread: bool
    accounts: list[Account_]
    last_status: Status_ | None = None


@router.get("/api/v1/conversations", response_model=list[Conversation_])
async def index(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Conversation_]:
    stmt = apply_pagination(
        select(AccountConversation).where(
            AccountConversation.account_id == viewer.id
        ),
        AccountConversation.id,
        params,
    )
    rows = (await session.execute(stmt)).scalars().all()
    ordered = maybe_reverse(rows, params)
    if not ordered:
        return []

    # Batch-fetch participants and last statuses.
    participant_ids: set[int] = set()
    status_ids: set[int] = set()
    for row in ordered:
        participant_ids.update(row.participant_account_ids or [])
        if row.last_status_id is not None:
            status_ids.add(row.last_status_id)

    accounts_by_id: dict[int, Account] = {}
    if participant_ids:
        accs = (
            await session.execute(
                select(Account).where(Account.id.in_(participant_ids))
            )
        ).unique().scalars().all()
        accounts_by_id = {a.id: a for a in accs}

    statuses_by_id: dict[int, Status] = {}
    if status_ids:
        sts = (
            await session.execute(
                select(Status).where(Status.id.in_(status_ids))
            )
        ).unique().scalars().all()
        statuses_by_id = {s.id: s for s in sts}

    relationships = await load_relationships(
        session, viewer.id, list(status_ids)
    )

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
    )
    if link:
        response.headers["Link"] = link

    out: list[Conversation_] = []
    for row in ordered:
        accts = [
            accounts_by_id[a]
            for a in (row.participant_account_ids or [])
            if a in accounts_by_id
        ]
        last = (
            statuses_by_id.get(row.last_status_id)
            if row.last_status_id is not None
            else None
        )
        out.append(
            Conversation_(
                id=str(row.id),
                unread=row.unread,
                accounts=[serialize_account(a) for a in accts],
                last_status=(
                    serialize_status(last, relationships=relationships)
                    if last is not None
                    else None
                ),
            )
        )
    return out


@router.post("/api/v1/conversations/{conversation_id}/read", response_model=Conversation_)
async def mark_read(
    conversation_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Conversation_:
    row = (
        await session.execute(
            select(AccountConversation).where(
                AccountConversation.id == conversation_id,
                AccountConversation.account_id == viewer.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    row.unread = False
    row.lock_version += 1
    await session.commit()
    return await _serialize_one(session, viewer, row)


@router.delete("/api/v1/conversations/{conversation_id}", status_code=status.HTTP_200_OK)
async def destroy(
    conversation_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(AccountConversation).where(
                AccountConversation.id == conversation_id,
                AccountConversation.account_id == viewer.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    await session.execute(
        delete(AccountConversation).where(AccountConversation.id == conversation_id)
    )
    await session.commit()
    return {}


async def _serialize_one(
    session, viewer: Account, row: AccountConversation
) -> Conversation_:
    accs = (
        await session.execute(
            select(Account).where(
                Account.id.in_(row.participant_account_ids or [])
            )
        )
    ).unique().scalars().all()
    last = None
    if row.last_status_id is not None:
        last = (
            await session.execute(
                select(Status).where(Status.id == row.last_status_id)
            )
        ).scalar_one_or_none()
    relationships = await load_relationships(
        session, viewer.id, [last.id] if last is not None else []
    )
    return Conversation_(
        id=str(row.id),
        unread=row.unread,
        accounts=[serialize_account(a) for a in accs],
        last_status=(
            serialize_status(last, relationships=relationships)
            if last is not None
            else None
        ),
    )


# ---------- /api/v1/timelines/direct ----------


@router.get("/api/v1/timelines/direct", response_model=list[Status_])
async def direct_timeline(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Status_]:
    """Direct messages where the viewer is either the author or a mentioned
    recipient.

    The legacy backend prefers `account_conversations.status_ids` for this
    lookup; we go to the source-of-truth via Mention + Status to keep the
    query straightforward.
    """
    viewer_mention_status_ids = select(Mention.status_id).where(
        Mention.account_id == viewer.id
    )
    stmt = select(Status).where(
        Status.visibility == Visibility.DIRECT.value,
        Status.deleted_at.is_(None),
        or_(
            Status.account_id == viewer.id,
            Status.id.in_(viewer_mention_status_ids),
        ),
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
    return [serialize_status(s, relationships=relationships) for s in ordered]
