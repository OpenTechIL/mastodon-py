"""`/api/v1/accounts/*` endpoints."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, or_, select

from app.python.auth.oauth import _resolve_client
from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.common.snowflake import now_id
from app.python.deps import CurrentAccount, DBSession, OptionalAuth
from app.python.models import (
    Account,
    AccountNote,
    AccountPin,
    Follow,
    OAuthAccessToken,
    Status,
    StatusPin,
    User,
    Visibility,
)
from app.python.models.account_stat import AccountStat
from app.python.policies.status_policy import _follows
from app.python.queue import Enqueuer, get_enqueuer
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.relationship import Relationship, serialize_relationship
from app.python.schemas.status import Status_, serialize_status
from app.python.services import blocks as block_service
from app.python.services import follows as follow_service
from app.python.services import mutes as mute_service
from app.python.services.account_relationships import load_account_relationships
from app.python.services.status_relationships import (
    load_relationships,
    status_ids_for_batch,
)
from app.python.settings import get_settings

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


class RegistrationBody(BaseModel):
    username: str
    email: str
    password: str
    agreement: bool = False
    locale: str = "en"
    reason: str | None = None


@router.post("", status_code=status.HTTP_200_OK)
async def register(
    body: RegistrationBody,
    session: DBSession,
    client_id: str | None = Query(default=None),
    client_secret: str | None = Query(default=None),
) -> dict:
    """Create a new account and return an OAuth token for it.

    Requires client_credentials in query or header. In open-registration
    mode (the current default) the account is immediately confirmed.
    """

    # Validate required fields
    if not body.username or not body.email or not body.password:
        raise HTTPException(status_code=422, detail={"error": "Validation failed"})
    if not body.agreement:
        raise HTTPException(status_code=422, detail={"error": "Agreement must be accepted"})

    # Username must be unique (case-insensitive)
    existing = (
        await session.execute(
            select(Account).where(
                Account.username == body.username,
                Account.domain.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=422, detail={"error": "Username is already taken"})

    # Email must be unique
    existing_user = (await session.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if existing_user:
        raise HTTPException(status_code=422, detail={"error": "Email is already in use"})

    settings = get_settings()
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    domain = settings.local_domain

    account = Account(
        id=now_id(),
        username=body.username,
        domain=None,
        display_name="",
        note="",
        uri="",
        url=None,
        locked=False,
        discoverable=False,
        indexable=False,
        memorial=False,
        fields=[],
        public_key="",
        private_key="",
        inbox_url="",
        shared_inbox_url="",
        header_remote_url="",
        created_at=now,
        updated_at=now,
    )
    session.add(account)
    await session.flush()

    # Update URI/URL now that we have the ID
    account.uri = f"https://{domain}/users/{body.username}"
    account.url = f"https://{domain}/@{body.username}"
    account.inbox_url = f"https://{domain}/users/{body.username}/inbox"

    stat = AccountStat(
        account_id=account.id,
        statuses_count=0,
        following_count=0,
        followers_count=0,
        created_at=now,
        updated_at=now,
    )
    session.add(stat)

    encrypted_password = bcrypt.hashpw(body.password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")

    user = User(
        id=now_id(),
        account_id=account.id,
        email=body.email,
        encrypted_password=encrypted_password,
        confirmed_at=now,  # auto-confirm in open registration
        approved=True,
        disabled=False,
        otp_required_for_login=False,
        locale=body.locale,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    await session.flush()

    # Issue an access token. Use client_credentials from query params if provided.
    token_scopes = "read write follow push"
    app_id: int | None = None
    if client_id and client_secret:
        try:
            app = await _resolve_client(session, client_id, client_secret)
            app_id = app.id
            token_scopes = app.scopes or token_scopes
        except Exception:
            pass  # proceed without app binding

    access_token = OAuthAccessToken(
        id=now_id(),
        token=secrets.token_hex(32),
        refresh_token=None,
        scopes=token_scopes,
        application_id=app_id,
        resource_owner_id=user.id,
        expires_in=None,
        revoked_at=None,
        created_at=now,
        last_used_at=None,
        last_used_ip=None,
    )
    session.add(access_token)
    await session.commit()

    return {
        "access_token": access_token.token,
        "token_type": "Bearer",
        "scope": access_token.scopes,
        "created_at": int(now.timestamp()),
    }


@router.get("/verify_credentials", response_model=Account_)
async def verify_credentials(account: CurrentAccount, session: DBSession) -> Account_:
    user = (await session.execute(select(User).where(User.account_id == account.id))).scalar_one_or_none()
    acc = serialize_account(account)
    acc.source = {
        "privacy": "public",
        "sensitive": False,
        "language": user.locale if user and user.locale else "en",
        "note": account.note or "",
        "fields": [{"name": str(f.get("name", "")), "value": str(f.get("value", ""))} for f in (account.fields or [])],
        "follow_requests_count": 0,
        "hide_collections": account.hide_collections,
        "discoverable": account.discoverable,
        "indexable": account.indexable,
        "attribution_domains": [],
        "quote_policy": "allow",
    }
    acc.role = {"id": "0", "name": "user", "permissions": "65536", "color": "", "highlighted": False}
    return acc


class _ProfileField(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    value: str = ""


def _coerce_bool(v: object) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


async def _save_account_image(account: Account, kind: str, file_bytes: bytes, content_type: str, filename: str) -> None:
    import os as _os

    from app.python.storage import get_storage

    ext = _os.path.splitext(filename)[1] or ".jpg"
    fname = f"original{ext}"
    storage_dir = "avatars" if kind == "avatar" else "headers"
    storage = get_storage()
    # Write both original and static variants; static is the same bytes for non-GIF.
    await storage.write(f"accounts/{storage_dir}/{account.id}/original/{fname}", file_bytes)
    await storage.write(f"accounts/{storage_dir}/{account.id}/static/{fname}", file_bytes)
    setattr(account, f"{kind}_file_name", fname)
    setattr(account, f"{kind}_content_type", content_type or "image/jpeg")


async def _apply_credentials_data(account: Account, data: dict) -> None:
    if "display_name" in data and data["display_name"] is not None:
        account.display_name = data["display_name"]
    if "note" in data and data["note"] is not None:
        account.note = data["note"]
    if "locked" in data and data["locked"] is not None:
        account.locked = bool(_coerce_bool(data["locked"]))
    if "bot" in data and data["bot"] is not None:
        account.actor_type = "Service" if _coerce_bool(data["bot"]) else "Person"
    if "discoverable" in data and data["discoverable"] is not None:
        account.discoverable = bool(_coerce_bool(data["discoverable"]))
    if "indexable" in data and data["indexable"] is not None:
        account.indexable = bool(_coerce_bool(data["indexable"]))
    if "hide_collections" in data and data["hide_collections"] is not None:
        account.hide_collections = bool(_coerce_bool(data["hide_collections"]))
    if "fields_attributes" in data and data["fields_attributes"] is not None:
        raw = data["fields_attributes"]
        if isinstance(raw, list):
            cleaned = [
                {"name": str(f.get("name", "")).strip(), "value": str(f.get("value", "")).strip()}
                for f in raw
                if isinstance(f, dict)
                if str(f.get("name", "")).strip() or str(f.get("value", "")).strip()
            ][:4]
            account.fields = cleaned


@router.patch("/update_credentials", response_model=Account_)
async def update_credentials(
    request: Request,
    session: DBSession,
    account: CurrentAccount,
) -> Account_:
    """Edit the caller's own profile.

    Accepts both application/json and multipart/form-data. The SPA sends
    multipart when uploading avatar/header; API clients and tests use JSON.
    """
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        data: dict = {k: v for k, v in form.items() if not hasattr(v, "read")}
        await _apply_credentials_data(account, data)

        for kind in ("avatar", "header"):
            file_field = form.get(kind)
            if file_field is not None and hasattr(file_field, "read"):
                file_bytes = await file_field.read()
                if file_bytes:
                    await _save_account_image(
                        account,
                        kind,
                        file_bytes,
                        getattr(file_field, "content_type", None) or "image/jpeg",
                        getattr(file_field, "filename", None) or f"{kind}.jpg",
                    )
    else:
        import json as _json

        body = await request.body()
        data = _json.loads(body) if body else {}
        await _apply_credentials_data(account, data)

    account.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)
    await session.commit()
    return serialize_account(account)


@router.get("/search", response_model=list[Account_])
async def search(
    session: DBSession,
    auth: OptionalAuth,
    q: str = Query(default=""),
    limit: int = Query(default=40, ge=1, le=80),
    following: bool = Query(default=False),
    resolve: bool = Query(default=False),
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
        followee_ids = select(Follow.target_account_id).where(Follow.account_id == auth.account.id)
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
        (await session.execute(select(Follow.target_account_id).where(Follow.account_id == viewer.id))).scalars().all()
    )
    viewer_followee_set = set(viewer_followee_ids)

    for target_id in id:
        if target_id == viewer.id or not viewer_followee_set:
            out.append({"id": str(target_id), "accounts": []})
            continue
        # Accounts in viewer's followee set that also follow the target.
        rows = (
            (
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
            )
            .unique()
            .scalars()
            .all()
        )
        out.append({"id": str(target_id), "accounts": [serialize_account(a) for a in rows]})
    return out


class _AccountNoteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    comment: str = ""


async def _load_target(session, account_id: int) -> Account:
    row = (await session.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()
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
        await follow_service.follow(session, source=viewer, target=target, enqueuer=enqueuer)
    except follow_service.SelfFollow as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
    except follow_service.BlockedFollow as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This action is not allowed") from exc
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
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
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found") from exc
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
        now = datetime.now(tz=UTC).replace(tzinfo=None)
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
        now = datetime.now(tz=UTC).replace(tzinfo=None)
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
    await follow_service.unfollow(session, source=viewer, target=target, enqueuer=enqueuer)
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


@router.post("/{account_id}/remove_from_followers", response_model=Relationship)
async def remove_from_followers(
    account_id: int,
    session: DBSession,
    viewer: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Relationship:
    """Remove the given account from the viewer's followers list."""
    follower = await _load_target(session, account_id)
    await follow_service.unfollow(session, source=follower, target=viewer, enqueuer=enqueuer)
    rels = await load_account_relationships(session, viewer.id, [account_id])
    return serialize_relationship(account_id, rels)


async def _allowed_visibilities(session, viewer_account_id: int | None, target_account_id: int) -> set[int]:
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
    only_media: bool = Query(default=False),
    pinned: bool = Query(default=False),
    tagged: str | None = Query(default=None),
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
        relationships = await load_relationships(session, viewer_account_id, status_ids_for_batch(pinned_rows))
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
        extra_query=_statuses_link_extras(exclude_replies=exclude_replies, exclude_reblogs=exclude_reblogs),
    )
    if link:
        response.headers["Link"] = link

    relationships = await load_relationships(session, viewer_account_id, status_ids_for_batch(ordered))
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
    if target.hide_collections and not (auth and auth.account and auth.account.id == target.id):
        return []

    if role == "followers":
        follow_filter = Follow.target_account_id == target.id
        account_filter_id = Follow.account_id
    else:
        follow_filter = Follow.account_id == target.id
        account_filter_id = Follow.target_account_id

    stmt = select(Follow.id, account_filter_id).where(follow_filter)
    stmt = apply_pagination(stmt, Follow.id, params)
    pairs = (await session.execute(stmt)).all()
    ordered_pairs = maybe_reverse([_FollowCursor(follow_id=row[0], account_id=row[1]) for row in pairs], params)
    if not ordered_pairs:
        return []

    account_ids = [p.account_id for p in ordered_pairs]
    rows = (await session.execute(select(Account).where(Account.id.in_(account_ids)))).unique().scalars().all()
    by_id = {row.id: row for row in rows}

    accounts = [
        by_id[p.account_id] for p in ordered_pairs if p.account_id in by_id and not by_id[p.account_id].suspended
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

    __slots__ = ("account_id", "follow_id")

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
