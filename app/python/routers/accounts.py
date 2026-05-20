"""`/api/v1/accounts/*` endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import or_, select

from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.deps import CurrentAccount, DBSession, OptionalAuth
from app.python.common.snowflake import now_id
from app.python.models import (
    Account,
    AccountNote,
    AccountPin,
    Follow,
    Status,
    StatusPin,
    Visibility,
)
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete
from app.python.policies.status_policy import _follows  # noqa: PLC2701
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.relationship import Relationship, serialize_relationship
from app.python.schemas.status import Status_, serialize_status
from app.python.queue import Enqueuer, get_enqueuer
from app.python.services import blocks as block_service
from app.python.services import follows as follow_service
from app.python.services import mutes as mute_service
from app.python.services.account_relationships import load_account_relationships
from app.python.services.status_relationships import (
    load_relationships,
    status_ids_for_batch,
)

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


@router.get("/verify_credentials", response_model=Account_)
async def verify_credentials(account: CurrentAccount) -> Account_:
    return serialize_account(account)


class _ProfileField(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    value: str = ""


class _UpdateCredentialsBody(BaseModel):
    """The subset of the Mastodon `update_credentials` body we accept now.

    `avatar` / `header` uploads need image variant generation and are
    deferred to the media pipeline phase. `source[*]` writes user
    preferences into `users.settings` which we don't yet parse.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    display_name: str | None = None
    note: str | None = None
    locked: bool | None = None
    bot: bool | None = None
    discoverable: bool | None = None
    indexable: bool | None = None
    hide_collections: bool | None = None
    fields_attributes: list[_ProfileField] | None = Field(
        default=None, alias="fields_attributes"
    )


@router.patch("/update_credentials", response_model=Account_)
async def update_credentials(
    body: _UpdateCredentialsBody,
    session: DBSession,
    account: CurrentAccount,
) -> Account_:
    """Edit the caller's own profile.

    Partial-update semantics — any field omitted from the body stays
    unchanged. `fields_attributes` is the full desired set; empty rows
    are dropped, the whole array is capped at four entries (matching
    `Account::DEFAULT_FIELDS_SIZE`).
    """
    if body.display_name is not None:
        account.display_name = body.display_name
    if body.note is not None:
        account.note = body.note
    if body.locked is not None:
        account.locked = body.locked
    if body.bot is not None:
        # `actor_type` is the storage column; "Service" maps to bot=true.
        account.actor_type = "Service" if body.bot else "Person"
    if body.discoverable is not None:
        account.discoverable = body.discoverable
    if body.indexable is not None:
        account.indexable = body.indexable
    if body.hide_collections is not None:
        account.hide_collections = body.hide_collections
    if body.fields_attributes is not None:
        cleaned = [
            {"name": f.name.strip(), "value": f.value.strip()}
            for f in body.fields_attributes
            if f.name.strip() or f.value.strip()
        ][:4]
        account.fields = cleaned

    account.updated_at = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    await session.commit()
    return serialize_account(account)


@router.get("/search", response_model=list[Account_])
async def search(
    session: DBSession,
    auth: OptionalAuth,
    q: str = Query(default=""),
    limit: int = Query(default=40, ge=1, le=80),
    following: bool = Query(default=False),
    resolve: bool = Query(default=False),  # noqa: ARG001 — webfinger deferred
) -> list[Account_]:
    """Substring search on username + display_name.

    `?resolve=true` would webfinger an `acct=user@domain.tld` we don't
    yet know; that pipeline ports with federation. For now resolve is
    accepted but ignored (returns the same local matches).
    """
    q = q.strip()
    if not q:
        return []

    needle = f"%{q.lstrip('@')}%"
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
        .limit(limit)
    )
    if following and auth and auth.account:
        followee_ids = select(Follow.target_account_id).where(
            Follow.account_id == auth.account.id
        )
        stmt = stmt.where(Account.id.in_(followee_ids))
    rows = (await session.execute(stmt)).unique().scalars().all()
    return [serialize_account(a) for a in rows]


@router.get("/lookup", response_model=Account_)
async def lookup(
    session: DBSession,
    acct: str = Query(...),
) -> Account_:
    """Mastodon-API `acct=` lookup.

    Accepts `username` (local) or `username@domain` (remote). Returns
    the cached Account row; no webfinger fetch is performed in this
    slice — unknown remotes 404.
    """
    name = acct.lstrip("@")
    username, _, domain = name.partition("@")
    if not username:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    if domain:
        stmt = select(Account).where(
            Account.username.ilike(username),
            Account.domain.ilike(domain),
        )
    else:
        stmt = select(Account).where(
            Account.username.ilike(username),
            Account.domain.is_(None),
        )
    row = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if row is None or row.suspended:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return serialize_account(row)


@router.get("/relationships", response_model=list[Relationship])
async def relationships(
    session: DBSession,
    viewer: CurrentAccount,
    id: list[int] = Query(default_factory=list, alias="id[]"),
) -> list[Relationship]:
    """Batch relationship lookup. Mastodon clients pass `id[]=…&id[]=…`."""
    rels = await load_account_relationships(session, viewer.id, id)
    return [serialize_relationship(account_id, rels) for account_id in id]


@router.get("/familiar_followers")
async def familiar_followers(
    session: DBSession,
    viewer: CurrentAccount,
    id: list[int] = Query(default_factory=list, alias="id[]"),
) -> list[dict[str, object]]:
    """For each target id, return up to 4 accounts the viewer follows
    who also follow the target. Self-target returns an empty list.
    """
    out: list[dict[str, object]] = []
    if not id:
        return out

    viewer_followee_ids = (
        await session.execute(
            select(Follow.target_account_id).where(Follow.account_id == viewer.id)
        )
    ).scalars().all()
    viewer_followee_set = set(viewer_followee_ids)

    for target_id in id:
        if target_id == viewer.id or not viewer_followee_set:
            out.append({"id": str(target_id), "accounts": []})
            continue
        # Accounts in viewer's followee set that also follow the target.
        rows = (
            await session.execute(
                select(Account)
                .join(Follow, Follow.account_id == Account.id)
                .where(
                    Follow.target_account_id == target_id,
                    Account.id.in_(viewer_followee_set),
                )
                .order_by(Follow.id.desc())
                .limit(4)
            )
        ).unique().scalars().all()
        out.append(
            {"id": str(target_id), "accounts": [serialize_account(a) for a in rows]}
        )
    return out


class _AccountNoteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    comment: str = ""


async def _load_target(session, account_id: int) -> Account:
    row = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if row is None or row.suspended:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return row


@router.get("/{account_id}", response_model=Account_)
async def show(account_id: int, session: DBSession) -> Account_:
    return serialize_account(await _load_target(session, account_id))


@router.post("/{account_id}/follow", response_model=Relationship)
async def follow(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Relationship:
    target = await _load_target(session, account_id)
    try:
        await follow_service.follow(
            session, source=viewer, target=target, enqueuer=enqueuer
        )
    except follow_service.SelfFollow as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Record not found"
        ) from exc
    except follow_service.BlockedFollow as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="This action is not allowed"
        ) from exc
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


class _MuteParams:
    """Body shape for POST /accounts/{id}/mute — Mastodon accepts both
    form-encoded and JSON. We model it as Query+body via FastAPI's
    Optional fields rather than a dedicated schema to mirror the
    minimal interface."""


@router.post("/{account_id}/block", response_model=Relationship)
async def block(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Relationship:
    target = await _load_target(session, account_id)
    try:
        await block_service.block(session, source=viewer, target=target)
    except block_service.SelfBlock as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Record not found"
        ) from exc
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/unblock", response_model=Relationship)
async def unblock(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Relationship:
    target = await _load_target(session, account_id)
    await block_service.unblock(session, source=viewer, target=target)
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/mute", response_model=Relationship)
async def mute(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
    notifications: bool = Query(default=True),
    duration: int = Query(default=0, ge=0),
) -> Relationship:
    target = await _load_target(session, account_id)
    try:
        await mute_service.mute(
            session,
            source=viewer,
            target=target,
            hide_notifications=notifications,
            duration_seconds=duration or None,
        )
    except mute_service.SelfMute as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Record not found"
        ) from exc
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/unmute", response_model=Relationship)
async def unmute(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Relationship:
    target = await _load_target(session, account_id)
    await mute_service.unmute(session, source=viewer, target=target)
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/note", response_model=Relationship)
async def set_note(
    account_id: int,
    body: _AccountNoteBody,
    session: DBSession,
    viewer: CurrentAccount,
) -> Relationship:
    """Upsert the caller's note about another account.

    Empty `comment` deletes the row, matching Mastodon's contract: the
    `note` field in the Relationship response goes back to `""`.
    """
    await _load_target(session, account_id)  # 404 on missing/suspended

    if not body.comment.strip():
        await session.execute(
            delete(AccountNote).where(
                AccountNote.account_id == viewer.id,
                AccountNote.target_account_id == account_id,
            )
        )
    else:
        existing = (
            await session.execute(
                select(AccountNote).where(
                    AccountNote.account_id == viewer.id,
                    AccountNote.target_account_id == account_id,
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        if existing is None:
            session.add(
                AccountNote(
                    id=now_id(),
                    account_id=viewer.id,
                    target_account_id=account_id,
                    comment=body.comment,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.comment = body.comment
            existing.updated_at = now
    await session.commit()

    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/pin", response_model=Relationship)
async def pin_account(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Relationship:
    """Endorse another account. Mastodon requires you to follow first.

    Self-pin → 404. Pinning someone you don't follow → 422 (matches
    the legacy `AccountPolicy#pin?` rejection).
    """
    if account_id == viewer.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    target = await _load_target(session, account_id)

    follow_exists = (
        await session.execute(
            select(Follow.id).where(
                Follow.account_id == viewer.id,
                Follow.target_account_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if follow_exists is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="You must be following this account to endorse it",
        )

    existing = (
        await session.execute(
            select(AccountPin).where(
                AccountPin.account_id == viewer.id,
                AccountPin.target_account_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        session.add(
            AccountPin(
                id=now_id(),
                account_id=viewer.id,
                target_account_id=target.id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/unpin", response_model=Relationship)
async def unpin_account(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Relationship:
    await _load_target(session, account_id)
    await session.execute(
        delete(AccountPin).where(
            AccountPin.account_id == viewer.id,
            AccountPin.target_account_id == account_id,
        )
    )
    await session.commit()
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/unfollow", response_model=Relationship)
async def unfollow(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Relationship:
    target = await _load_target(session, account_id)
    await follow_service.unfollow(
        session, source=viewer, target=target, enqueuer=enqueuer
    )
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


async def _allowed_visibilities(
    session, viewer_account_id: int | None, target_account_id: int
) -> set[int]:
    """Compute the set of visibility values the viewer is permitted to see.

    DIRECT is intentionally excluded even when the viewer is the author —
    profile-statuses isn't a direct-message inbox.
    """
    allowed = {Visibility.PUBLIC.value, Visibility.UNLISTED.value}
    if viewer_account_id is None:
        return allowed
    if viewer_account_id == target_account_id:
        # Author viewing own profile: include private (their own followers-only
        # posts). Direct still excluded so the profile doesn't leak inboxes.
        allowed.add(Visibility.PRIVATE.value)
        return allowed
    if await _follows(session, viewer_account_id, target_account_id):
        allowed.add(Visibility.PRIVATE.value)
    return allowed


@router.get("/{account_id}/statuses", response_model=list[Status_])
async def account_statuses(
    account_id: int,
    request: Request,
    response: Response,
    session: DBSession,
    auth: OptionalAuth,
    params: Annotated[PageParams, Depends(page_params)],
    exclude_replies: bool = Query(default=False),
    exclude_reblogs: bool = Query(default=False),
    only_media: bool = Query(default=False),  # noqa: ARG001 — media isn't modeled yet
    pinned: bool = Query(default=False),
    tagged: str | None = Query(default=None),  # noqa: ARG001 — tags not modeled yet
) -> list[Status_]:
    target = await _load_target(session, account_id)
    viewer_account_id = auth.account.id if (auth and auth.account) else None

    allowed = await _allowed_visibilities(session, viewer_account_id, target.id)

    if pinned:
        pin_stmt = (
            select(Status)
            .join(StatusPin, StatusPin.status_id == Status.id)
            .where(
                StatusPin.account_id == target.id,
                Status.deleted_at.is_(None),
                Status.visibility.in_(allowed),
            )
            .order_by(StatusPin.id.desc())
            .limit(params.limit)
        )
        pinned_rows = (await session.execute(pin_stmt)).unique().scalars().all()
        relationships = await load_relationships(
            session, viewer_account_id, status_ids_for_batch(pinned_rows)
        )
        return [serialize_status(row, relationships=relationships) for row in pinned_rows]

    stmt = select(Status).where(
        Status.account_id == target.id,
        Status.deleted_at.is_(None),
        Status.visibility.in_(allowed),
    )
    if exclude_replies:
        stmt = stmt.where(
            or_(
                Status.reply.is_(False),
                Status.in_reply_to_account_id == Status.account_id,
            )
        )
    if exclude_reblogs:
        stmt = stmt.where(Status.reblog_of_id.is_(None))

    stmt = apply_pagination(stmt, Status.id, params)
    rows = (await session.execute(stmt)).unique().scalars().all()
    ordered = maybe_reverse(rows, params)

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
        extra_query=_statuses_link_extras(
            exclude_replies=exclude_replies, exclude_reblogs=exclude_reblogs
        ),
    )
    if link:
        response.headers["Link"] = link

    relationships = await load_relationships(
        session, viewer_account_id, status_ids_for_batch(ordered)
    )
    return [serialize_status(row, relationships=relationships) for row in ordered]


def _statuses_link_extras(*, exclude_replies: bool, exclude_reblogs: bool) -> dict[str, str]:
    out: dict[str, str] = {}
    if exclude_replies:
        out["exclude_replies"] = "true"
    if exclude_reblogs:
        out["exclude_reblogs"] = "true"
    return out


async def _follow_page(
    *,
    request: Request,
    response: Response,
    session,
    auth,
    target: Account,
    params: PageParams,
    role: str,  # "followers" or "following"
) -> list[Account_]:
    """Shared listing for /followers and /following.

    Cursor is on Follow.id (Mastodon's contract — most recent follow first);
    response body is the related Account.
    """
    if (
        target.hide_collections
        and not (auth and auth.account and auth.account.id == target.id)
    ):
        return []

    if role == "followers":
        follow_filter = Follow.target_account_id == target.id
        account_filter_id = Follow.account_id
    else:
        follow_filter = Follow.account_id == target.id
        account_filter_id = Follow.target_account_id

    stmt = (
        select(Follow.id, account_filter_id)
        .where(follow_filter)
    )
    stmt = apply_pagination(stmt, Follow.id, params)
    pairs = (await session.execute(stmt)).all()
    ordered_pairs = maybe_reverse(
        [_FollowCursor(follow_id=row[0], account_id=row[1]) for row in pairs], params
    )
    if not ordered_pairs:
        return []

    account_ids = [p.account_id for p in ordered_pairs]
    rows = (
        await session.execute(select(Account).where(Account.id.in_(account_ids)))
    ).unique().scalars().all()
    by_id = {row.id: row for row in rows}

    accounts = [
        by_id[p.account_id]
        for p in ordered_pairs
        if p.account_id in by_id and not by_id[p.account_id].suspended
    ]

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered_pairs,
        params,
        id_attr="follow_id",
    )
    if link:
        response.headers["Link"] = link

    return [serialize_account(a) for a in accounts]


class _FollowCursor:
    """Pagination row carrying the underlying Follow.id (the cursor key)
    and the related Account.id (what we render)."""

    __slots__ = ("follow_id", "account_id")

    def __init__(self, follow_id: int, account_id: int) -> None:
        self.follow_id = follow_id
        self.account_id = account_id


@router.get("/{account_id}/followers", response_model=list[Account_])
async def followers(
    account_id: int,
    request: Request,
    response: Response,
    session: DBSession,
    auth: OptionalAuth,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Account_]:
    target = await _load_target(session, account_id)
    return await _follow_page(
        request=request,
        response=response,
        session=session,
        auth=auth,
        target=target,
        params=params,
        role="followers",
    )


@router.get("/{account_id}/following", response_model=list[Account_])
async def following(
    account_id: int,
    request: Request,
    response: Response,
    session: DBSession,
    auth: OptionalAuth,
    params: Annotated[PageParams, Depends(page_params)],
) -> list[Account_]:
    target = await _load_target(session, account_id)
    return await _follow_page(
        request=request,
        response=response,
        session=session,
        auth=auth,
        target=target,
        params=params,
        role="following",
    )
