"""`/api/v1/statuses/*` endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.python.deps import CurrentAccount, DBSession, OptionalAuth
from app.python.models import Account, Favourite, Status, StatusEdit
from app.python.policies.status_policy import visible_to
from app.python.queue import Enqueuer, get_enqueuer
from app.python.schemas.status import Status_, serialize_status
from app.python.schemas.status_create import StatusCreate
from app.python.schemas.status_edit import (
    StatusEdit_,
    StatusSource,
    serialize_edit,
    serialize_source,
)
from app.python.services import bookmarks as bookmark_service
from app.python.services import delete_status as delete_status_service
from app.python.services import favourites as favourite_service
from app.python.services import pins as pin_service
from app.python.services import post_status as post_status_service
from app.python.services import reblogs as reblog_service
from app.python.services import update_status as update_status_service
from app.python.services.filter_application import load_filters_for
from app.python.services.status_relationships import load_relationships

router = APIRouter(prefix="/api/v1/statuses", tags=["statuses"])


async def _load_visible_status(
    session,
    status_id: int,
    viewer_account_id: int | None,
) -> Status:
    row = (await session.execute(select(Status).where(Status.id == status_id))).scalar_one_or_none()
    if row is None or not await visible_to(session, row, viewer_account_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return row


@router.post("", response_model=Status_, status_code=status.HTTP_200_OK)
async def create(
    body: StatusCreate,
    session: DBSession,
    account: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Status_:
    poll_spec = (
        post_status_service.PollSpec(
            options=body.poll.options,
            expires_in=body.poll.expires_in,
            multiple=body.poll.multiple,
            hide_totals=body.poll.hide_totals,
        )
        if body.poll is not None
        else None
    )
    row = await post_status_service.post_status(
        session,
        author=account,
        text=body.status,
        visibility=body.visibility,
        sensitive=body.sensitive,
        spoiler_text=body.spoiler_text,
        language=body.language,
        in_reply_to_id=body.in_reply_to_id,
        media_ids=body.media_ids,
        poll=poll_spec,
        enqueuer=enqueuer,
    )
    # Re-query so eager-joined relationships (account, stat, reblog) are
    # materialised on the freshly created row. Constructor-built rows
    # skip the lazy-load hooks. Capture the id first; the service has
    # committed by now, so the original row object's attributes may have
    # been expired by the commit if expire_on_commit is on.
    new_id = row.id
    account_id = account.id
    hydrated = (await session.execute(select(Status).where(Status.id == new_id))).scalar_one()
    relationships = await load_relationships(session, account_id, [new_id])
    return serialize_status(hydrated, relationships=relationships)


@router.put("/{status_id}", response_model=Status_)
async def update(
    status_id: int,
    body: StatusCreate,
    session: DBSession,
    account: CurrentAccount,
) -> Status_:
    row = (await session.execute(select(Status).where(Status.id == status_id))).scalar_one_or_none()
    if row is None or row.discarded:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    try:
        await update_status_service.update_status(
            session,
            author=account,
            status=row,
            update=update_status_service.StatusUpdate(
                text=body.status if body.status else None,
                spoiler_text=body.spoiler_text,
                sensitive=body.sensitive,  # Optional[bool]; None means "don't override"
                language=body.language,
            ),
        )
    except update_status_service.StatusForbidden as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc

    relationships = await load_relationships(session, account.id, [row.id])
    return serialize_status(row, relationships=relationships)


@router.get("/{status_id}/context")
async def context(
    status_id: int,
    session: DBSession,
    auth: OptionalAuth,
) -> dict[str, list[Status_]]:
    """Reply tree for a status: ancestors (up the in_reply_to chain) +
    descendants (down via replies).

    Anonymous viewers see a shallower tree (40 ancestors / 60 descendants
    / 6 levels deep) to keep the unauthenticated cost bounded. Authed
    viewers get the full `CONTEXT_LIMIT` of 4096 each (matching Rails).
    """
    row = (await session.execute(select(Status).where(Status.id == status_id))).scalar_one_or_none()
    if row is None or row.discarded:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    viewer_account_id = auth.account.id if (auth and auth.account) else None
    if not await visible_to(session, row, viewer_account_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    if viewer_account_id is None:
        ancestors_limit = 40
        descendants_limit = 60
        depth_limit: int | None = 6
    else:
        ancestors_limit = 4096
        descendants_limit = 4096
        depth_limit = None

    ancestors = await _ancestors(session, row, limit=ancestors_limit, viewer_account_id=viewer_account_id)
    descendants = await _descendants(
        session,
        row,
        limit=descendants_limit,
        depth_limit=depth_limit,
        viewer_account_id=viewer_account_id,
    )

    ids_to_load = [s.id for s in ancestors] + [s.id for s in descendants]
    relationships = await load_relationships(session, viewer_account_id, ids_to_load)
    return {
        "ancestors": [serialize_status(s, relationships=relationships) for s in ancestors],
        "descendants": [serialize_status(s, relationships=relationships) for s in descendants],
    }


async def _ancestors(
    session,
    status: Status,
    *,
    limit: int,
    viewer_account_id: int | None,
) -> list[Status]:
    chain: list[Status] = []
    current_parent_id = status.in_reply_to_id
    while current_parent_id is not None and len(chain) < limit:
        parent = (await session.execute(select(Status).where(Status.id == current_parent_id))).scalar_one_or_none()
        if parent is None or parent.discarded:
            break
        if await visible_to(session, parent, viewer_account_id):
            chain.append(parent)
        current_parent_id = parent.in_reply_to_id
    # Mastodon returns ancestors from oldest to newest, so the root is first.
    chain.reverse()
    return chain


async def _descendants(
    session,
    status: Status,
    *,
    limit: int,
    depth_limit: int | None,
    viewer_account_id: int | None,
) -> list[Status]:
    """Breadth-first walk down the reply tree.

    Returned list is depth-then-id-asc; matches Mastodon's rendering
    expectation that branches are grouped and replies within a branch
    are chronological.
    """
    out: list[Status] = []
    frontier: list[int] = [status.id]
    depth = 0
    while frontier and len(out) < limit:
        if depth_limit is not None and depth >= depth_limit:
            break
        rows = (
            (
                await session.execute(
                    select(Status)
                    .where(
                        Status.in_reply_to_id.in_(frontier),
                        Status.deleted_at.is_(None),
                    )
                    .order_by(Status.id.asc())
                )
            )
            .unique()
            .scalars()
            .all()
        )
        next_frontier: list[int] = []
        for child in rows:
            if not await visible_to(session, child, viewer_account_id):
                continue
            out.append(child)
            next_frontier.append(child.id)
            if len(out) >= limit:
                break
        frontier = next_frontier
        depth += 1
    return out


@router.get("/{status_id}/source", response_model=StatusSource)
async def source(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> StatusSource:
    row = (await session.execute(select(Status).where(Status.id == status_id))).scalar_one_or_none()
    if row is None or row.discarded or row.account_id != account.id:
        # Source is author-only — it's only useful for the edit dialog.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return serialize_source(row)


@router.get("/{status_id}/history", response_model=list[StatusEdit_])
async def history(
    status_id: int,
    session: DBSession,
    auth: OptionalAuth,
) -> list[StatusEdit_]:
    row = (await session.execute(select(Status).where(Status.id == status_id))).scalar_one_or_none()
    viewer_account_id = auth.account.id if (auth and auth.account) else None
    if row is None or not await visible_to(session, row, viewer_account_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    edits = (
        (
            await session.execute(
                select(StatusEdit)
                .where(StatusEdit.status_id == status_id)
                # Order by creation time first; snowflake-id tail randomness
                # can invert two writes within the same second.
                .order_by(StatusEdit.created_at.asc(), StatusEdit.id.asc())
            )
        )
        .scalars()
        .all()
    )

    # Append the current state as the final history entry — mirrors
    # Mastodon's behavior so the client renders a linear "this revision
    # → that revision → … → current" list.
    history_rows = list(edits)
    return [serialize_edit(e, row.account) for e in history_rows] + [_current_as_edit(row, row.account)]


def _current_as_edit(status: Status, author: Account) -> StatusEdit_:
    from app.python.lib.html import status_content_format
    from app.python.schemas.account import serialize_account

    return StatusEdit_(
        content=status_content_format(status.text),
        spoiler_text=status.spoiler_text,
        sensitive=status.sensitive,
        created_at=status.edited_at or status.created_at,
        account=serialize_account(author),
    )


@router.delete("/{status_id}", response_model=Status_)
async def destroy(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> Status_:
    row = (await session.execute(select(Status).where(Status.id == status_id))).scalar_one_or_none()
    if row is None or row.discarded:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    try:
        await delete_status_service.delete_status(session, author=account, status=row)
    except delete_status_service.StatusForbidden as exc:
        # Mastodon serves 404 on non-author delete to keep visibility
        # information from leaking.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    relationships = await load_relationships(session, account.id, [row.id])
    return serialize_status(row, relationships=relationships)


@router.get("/{status_id}", response_model=Status_)
async def show(
    status_id: int,
    session: DBSession,
    auth: OptionalAuth,
) -> Status_:
    viewer_account_id = auth.account.id if (auth and auth.account) else None
    row = await _load_visible_status(session, status_id, viewer_account_id)
    relationships = await load_relationships(session, viewer_account_id, [row.id])
    filter_checks = await load_filters_for(session, viewer_account_id, "thread")
    return serialize_status(row, relationships=relationships, filter_checks=filter_checks)


@router.post("/{status_id}/favourite", response_model=Status_)
async def favourite(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Status_:
    row = await _load_visible_status(session, status_id, account.id)
    try:
        await favourite_service.favourite(session, account=account, status=row, enqueuer=enqueuer)
    except favourite_service.StatusNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    await session.refresh(row, ["stat"])
    relationships = await load_relationships(session, account.id, [row.id])
    return serialize_status(row, relationships=relationships)


@router.post("/{status_id}/unfavourite", response_model=Status_)
async def unfavourite(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Status_:
    row = await _load_visible_status(session, status_id, account.id)
    await favourite_service.unfavourite(session, account=account, status=row, enqueuer=enqueuer)
    await session.refresh(row, ["stat"])
    relationships = await load_relationships(session, account.id, [row.id])
    return serialize_status(row, relationships=relationships)


@router.post("/{status_id}/bookmark", response_model=Status_)
async def bookmark(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> Status_:
    row = await _load_visible_status(session, status_id, account.id)
    try:
        await bookmark_service.bookmark(session, account=account, status=row)
    except favourite_service.StatusNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    relationships = await load_relationships(session, account.id, [row.id])
    return serialize_status(row, relationships=relationships)


@router.post("/{status_id}/unbookmark", response_model=Status_)
async def unbookmark(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> Status_:
    row = await _load_visible_status(session, status_id, account.id)
    await bookmark_service.unbookmark(session, account=account, status=row)
    relationships = await load_relationships(session, account.id, [row.id])
    return serialize_status(row, relationships=relationships)


@router.post("/{status_id}/pin", response_model=Status_)
async def pin(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> Status_:
    row = (await session.execute(select(Status).where(Status.id == status_id))).scalar_one_or_none()
    if row is None or row.discarded:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    try:
        await pin_service.pin(session, author=account, status=row)
    except pin_service.StatusForbidden as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    relationships = await load_relationships(session, account.id, [row.id])
    return serialize_status(row, relationships=relationships)


@router.post("/{status_id}/unpin", response_model=Status_)
async def unpin(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> Status_:
    row = (await session.execute(select(Status).where(Status.id == status_id))).scalar_one_or_none()
    if row is None or row.discarded:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    try:
        await pin_service.unpin(session, author=account, status=row)
    except pin_service.StatusForbidden as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    relationships = await load_relationships(session, account.id, [row.id])
    return serialize_status(row, relationships=relationships)


@router.post("/{status_id}/reblog", response_model=Status_)
async def reblog(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Status_:
    parent = await _load_visible_status(session, status_id, account.id)
    parent_id = parent.id  # capture before expire_all invalidates identity-map state
    try:
        wrapper_id = (await reblog_service.reblog(session, account=account, status=parent, enqueuer=enqueuer)).id
    except favourite_service.StatusNotFound as exc:
        # Parent isn't reblogable (private/direct) — same response shape
        # as "not found" so callers can't probe private posts.
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    # The service commits and bumps counters; expire identity-map state so
    # the wrapper's nested `reblog` and its `stat` reflect the new counts
    # when we re-read them below.
    session.expire_all()
    hydrated = (await session.execute(select(Status).where(Status.id == wrapper_id))).scalar_one()
    relationships = await load_relationships(session, account.id, [wrapper_id, parent_id])
    return serialize_status(hydrated, relationships=relationships)


@router.post("/{status_id}/unreblog", response_model=Status_)
async def unreblog(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Status_:
    """Mastodon returns the original status (not the discarded wrapper)."""
    parent = await _load_visible_status(session, status_id, account.id)
    await reblog_service.unreblog(session, account=account, status=parent, enqueuer=enqueuer)
    await session.refresh(parent, ["stat"])
    relationships = await load_relationships(session, account.id, [parent.id])
    return serialize_status(parent, relationships=relationships)


@router.get("/{status_id}/favourited_by", response_model=list[Any])
async def favourited_by(
    status_id: int,
    session: DBSession,
    viewer: OptionalAuth,
    max_id: int | None = Query(default=None),
    since_id: int | None = Query(default=None),
    limit: int = Query(default=40, ge=1, le=80),
) -> list[Any]:
    """Accounts that favourited the status."""
    from app.python.schemas.account import serialize_account

    viewer_id = viewer.account.id if (viewer and viewer.account) else None
    st = await _load_visible_status(session, status_id, viewer_id)
    stmt = (
        select(Account, Favourite.id.label("fav_id"))
        .join(Favourite, Favourite.account_id == Account.id)
        .where(Favourite.status_id == st.id)
    )
    if max_id is not None:
        stmt = stmt.where(Favourite.id < max_id)
    if since_id is not None:
        stmt = stmt.where(Favourite.id > since_id)
    stmt = stmt.order_by(Favourite.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [serialize_account(acc) for acc, _ in rows]


@router.get("/{status_id}/reblogged_by", response_model=list[Any])
async def reblogged_by(
    status_id: int,
    session: DBSession,
    viewer: OptionalAuth,
    max_id: int | None = Query(default=None),
    since_id: int | None = Query(default=None),
    limit: int = Query(default=40, ge=1, le=80),
) -> list[Any]:
    """Accounts that reblogged the status."""
    from app.python.schemas.account import serialize_account

    viewer_id = viewer.account.id if (viewer and viewer.account) else None
    st = await _load_visible_status(session, status_id, viewer_id)
    stmt = select(Account).join(Status, Status.account_id == Account.id).where(Status.reblog_of_id == st.id)
    if max_id is not None:
        stmt = stmt.where(Status.id < max_id)
    if since_id is not None:
        stmt = stmt.where(Status.id > since_id)
    stmt = stmt.order_by(Status.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).unique().scalars().all()
    return [serialize_account(acc) for acc in rows]


@router.post("/{status_id}/mute", response_model=Status_)
async def mute_status(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> Status_:
    """Mute notifications for a conversation thread. No-op until status_mutes table is ported."""
    st = await _load_visible_status(session, status_id, account.id)
    relationships = await load_relationships(session, account.id, [st.id])
    return serialize_status(st, relationships=relationships)


@router.post("/{status_id}/unmute", response_model=Status_)
async def unmute_status(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> Status_:
    """Unmute notifications for a conversation thread."""
    st = await _load_visible_status(session, status_id, account.id)
    relationships = await load_relationships(session, account.id, [st.id])
    return serialize_status(st, relationships=relationships)


@router.get("/{status_id}/translate", response_model=dict[str, Any])
async def translate_status(
    status_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> dict[str, Any]:
    """Translation stub — returns empty until translation service is ported."""
    await _load_visible_status(session, status_id, account.id)
    return {
        "content": "",
        "spoiler_text": "",
        "poll": None,
        "media_attachments": [],
        "detected_source_language": None,
        "provider": None,
    }


@router.get("/{status_id}/quotes", response_model=list[Any])
async def status_quotes(
    status_id: int,
    session: DBSession,
    viewer: OptionalAuth,
    max_id: int | None = Query(default=None),
    since_id: int | None = Query(default=None),
    limit: int = Query(default=40, ge=1, le=80),
) -> list[Any]:
    """Statuses that quote this status."""
    viewer_id = viewer.account.id if (viewer and viewer.account) else None
    await _load_visible_status(session, status_id, viewer_id)
    return []
