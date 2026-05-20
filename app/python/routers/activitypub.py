"""ActivityPub inbox endpoints.

  - `POST /inbox` — shared inbox; accepts activities for any local
    actor in this instance. Mastodon's batched-delivery target.
  - `POST /users/{username}/inbox` — per-actor inbox; the URL the
    remote server fetched from the local actor's profile JSON.

Both endpoints:

  1. Pull the raw request body — signature verification needs the
    untouched bytes.
  2. Call `verify_signed_request`. On failure, 401.
  3. (This slice) return 202 Accepted. Activity dispatch
     (`Create` / `Like` / `Announce` / `Delete` / `Follow` handlers)
     ports in subsequent slices; the response code is already correct
     because we're acknowledging receipt, not completion.

Per-actor inbox additionally 404s for unknown usernames so we don't
silently accept POSTs to arbitrary URLs — even though signature
verification would normally fail, returning 404 early is honest.
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select

from app.python.deps import DBSession, HttpClient
from app.python.federation.activity import dispatch as dispatch_activity
from app.python.federation.serializers import serialize_create_activity, serialize_note
from app.python.federation.signed_request import verify_signed_request
from app.python.lib.asset_urls import _asset_host, account_uri, avatar_url, header_url  # noqa: PLC2701
from app.python.models import Account, Follow, Status, StatusPin, Visibility
from app.python.queue import Enqueuer, get_enqueuer

router = APIRouter(tags=["activitypub"])

# Mastodon emits `application/activity+json` (with the AS2 + security
# JSON-LD contexts) for actor / object responses. Most peers accept it
# regardless of the more-strict `application/ld+json; profile=...`.
_AP_MEDIA_TYPE = "application/activity+json"
_AS2_CONTEXT = "https://www.w3.org/ns/activitystreams"
_SECURITY_CONTEXT = "https://w3id.org/security/v1"


def _actor_json(account: Account) -> dict:
    """Build the AP Person object for a local actor."""
    host = _asset_host()
    actor_url = f"{host}/users/{account.username}"
    body: dict = {
        "@context": [
            _AS2_CONTEXT,
            _SECURITY_CONTEXT,
            {
                "toot": "http://joinmastodon.org/ns#",
                "Hashtag": "as:Hashtag",
                "sensitive": "as:sensitive",
                "manuallyApprovesFollowers": "as:manuallyApprovesFollowers",
                "movedTo": {"@id": "as:movedTo", "@type": "@id"},
                "alsoKnownAs": {"@id": "as:alsoKnownAs", "@type": "@id"},
                "indexable": "toot:indexable",
                "discoverable": "toot:discoverable",
                "suspended": "toot:suspended",
                "memorial": "toot:memorial",
                "schema": "http://schema.org#",
                "PropertyValue": "schema:PropertyValue",
                "value": "schema:value",
            },
        ],
        "id": actor_url,
        "type": account.actor_type or "Person",
        "preferredUsername": account.username,
        "name": account.display_name or account.username,
        "summary": account.note or "",
        "url": f"{host}/@{account.username}",
        "inbox": f"{actor_url}/inbox",
        "outbox": f"{actor_url}/outbox",
        "followers": f"{actor_url}/followers",
        "following": f"{actor_url}/following",
        "featured": f"{actor_url}/featured",
        "endpoints": {
            "sharedInbox": f"{host}/inbox",
        },
        "publicKey": {
            "id": f"{actor_url}#main-key",
            "owner": actor_url,
            "publicKeyPem": account.public_key or "",
        },
        "manuallyApprovesFollowers": account.locked,
        "discoverable": bool(account.discoverable),
        "indexable": account.indexable,
    }

    # Actor icon (avatar)
    av = avatar_url(account)
    body["icon"] = {
        "type": "Image",
        "mediaType": account.avatar_content_type or "image/png",
        "url": av,
    }

    # Actor image (header banner)
    hd = header_url(account)
    body["image"] = {
        "type": "Image",
        "mediaType": account.header_content_type or "image/png",
        "url": hd,
    }

    # Profile fields as schema:PropertyValue attachment array
    fields = account.fields or []
    if fields:
        body["attachment"] = [
            {
                "type": "PropertyValue",
                "name": f.get("name", ""),
                "value": f.get("value", ""),
            }
            for f in fields
        ]

    # published date
    if account.created_at:
        body["published"] = account.created_at.isoformat(timespec="seconds") + "Z"

    return body


def _wants_html(accept: str | None) -> bool:
    """Return True when the client prefers text/html over activity+json."""
    if not accept:
        return False
    # Browsers send Accept: text/html,... with high q-value.
    # AP clients send application/activity+json or application/ld+json.
    accept_lower = accept.lower()
    has_html = "text/html" in accept_lower
    has_ap = "activity+json" in accept_lower or "ld+json" in accept_lower
    return has_html and not has_ap


@router.get("/users/{username}")
async def actor(username: str, request: Request, session: DBSession) -> Response:
    """Serve the AP Person object for a local actor.

    Content negotiation: browsers (Accept: text/html) are redirected to
    the profile HTML page. AP clients receive application/activity+json.
    """
    row = (
        await session.execute(
            select(Account).where(
                Account.username == username, Account.domain.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Record not found"
        )
    accept = request.headers.get("accept", "")
    if _wants_html(accept):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(
            url=f"{_asset_host()}/@{row.username}",
            status_code=302,
        )
    return Response(
        content=json.dumps(_actor_json(row)),
        media_type=_AP_MEDIA_TYPE,
    )


async def _verify_or_401(
    request: Request, session, http_client
) -> tuple[bytes, str]:
    """Verify the signed POST. Raises 401 on failure; returns
    `(body, actor_url)` on success."""
    body = await request.body()
    actor_url = await verify_signed_request(
        method=request.method,
        path=request.url.path,
        headers=dict(request.headers),
        body=body,
        session=session,
        http_client=http_client,
    )
    if actor_url is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Request signature could not be verified",
        )
    return body, actor_url


async def _dispatch_body(
    session, http_client, enqueuer, actor_url: str, body: bytes
) -> None:
    """Parse the verified body and route to a handler. Bad JSON is a
    no-op — the inbox already returned 202 conceptually."""
    try:
        activity = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(activity, dict):
        return
    await dispatch_activity(
        session=session,
        http_client=http_client,
        enqueuer=enqueuer,
        actor_url=actor_url,
        activity=activity,
    )


@router.post("/inbox", status_code=status.HTTP_202_ACCEPTED)
async def shared_inbox(
    request: Request,
    session: DBSession,
    http_client: HttpClient,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Response:
    body, actor_url = await _verify_or_401(request, session, http_client)
    await _dispatch_body(session, http_client, enqueuer, actor_url, body)
    return Response(status_code=status.HTTP_202_ACCEPTED)


# ---------- Collections (followers / following) ----------

_PAGE_SIZE = 40

CollectionKind = Literal["followers", "following"]


async def _load_local_actor(session, username: str) -> Account:
    row = (
        await session.execute(
            select(Account).where(
                Account.username == username, Account.domain.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Record not found"
        )
    return row


def _collection_endpoint(account: Account, kind: CollectionKind) -> str:
    return f"{_asset_host()}/users/{account.username}/{kind}"


async def _count(session, account: Account, kind: CollectionKind) -> int:
    if kind == "followers":
        stmt = select(func.count()).select_from(Follow).where(
            Follow.target_account_id == account.id
        )
    else:
        stmt = select(func.count()).select_from(Follow).where(
            Follow.account_id == account.id
        )
    return int((await session.execute(stmt)).scalar_one())


async def _page_items(
    session, account: Account, kind: CollectionKind, *, page: int
) -> list[str]:
    """Return the actor URIs on `page` (1-indexed) for the collection.

    Locals get a synthesized URI via `account_uri`; remotes use their
    stored `Account.uri`. Page boundaries follow Mastodon's `_PAGE_SIZE`
    so cross-implementation pagination stays consistent.
    """
    offset = max(0, (page - 1) * _PAGE_SIZE)
    if kind == "followers":
        stmt = (
            select(Account)
            .join(Follow, Follow.account_id == Account.id)
            .where(Follow.target_account_id == account.id)
            .order_by(Follow.id.desc())
            .offset(offset)
            .limit(_PAGE_SIZE)
        )
    else:
        stmt = (
            select(Account)
            .join(Follow, Follow.target_account_id == Account.id)
            .where(Follow.account_id == account.id)
            .order_by(Follow.id.desc())
            .offset(offset)
            .limit(_PAGE_SIZE)
        )
    rows = (await session.execute(stmt)).unique().scalars().all()
    return [account_uri(a) for a in rows]


def _collection_root(
    account: Account, kind: CollectionKind, *, total: int
) -> dict:
    endpoint = _collection_endpoint(account, kind)
    return {
        "@context": _AS2_CONTEXT,
        "id": endpoint,
        "type": "OrderedCollection",
        "totalItems": total,
        "first": f"{endpoint}?page=1",
    }


def _collection_page(
    account: Account,
    kind: CollectionKind,
    *,
    total: int,
    page: int,
    items: list[str],
) -> dict:
    endpoint = _collection_endpoint(account, kind)
    body: dict = {
        "@context": _AS2_CONTEXT,
        "id": f"{endpoint}?page={page}",
        "type": "OrderedCollectionPage",
        "partOf": endpoint,
        "totalItems": total,
        "orderedItems": items,
    }
    # Mastodon omits `next` on the final page so a crawling peer knows
    # to stop. If we filled this page, there *might* be more.
    if len(items) == _PAGE_SIZE:
        body["next"] = f"{endpoint}?page={page + 1}"
    if page > 1:
        body["prev"] = f"{endpoint}?page={page - 1}"
    return body


async def _serve_collection(
    session, username: str, kind: CollectionKind, page: str | None
) -> Response:
    account = await _load_local_actor(session, username)
    # `hide_collections` privacy switch: emit a zero-count root, no
    # paginated items. Peers see the collection exists but can't
    # enumerate it.
    if account.hide_collections:
        return Response(
            content=json.dumps(_collection_root(account, kind, total=0)),
            media_type=_AP_MEDIA_TYPE,
        )
    total = await _count(session, account, kind)
    if page is None:
        body = _collection_root(account, kind, total=total)
    else:
        try:
            page_num = int(page)
        except ValueError:
            page_num = 1
        page_num = max(1, page_num)
        items = await _page_items(session, account, kind, page=page_num)
        body = _collection_page(
            account, kind, total=total, page=page_num, items=items
        )
    return Response(content=json.dumps(body), media_type=_AP_MEDIA_TYPE)


# ---------- Outbox ----------


_OUTBOX_VISIBILITIES = (Visibility.PUBLIC.value, Visibility.UNLISTED.value)


async def _outbox_count(session, account: Account) -> int:
    stmt = select(func.count()).select_from(Status).where(
        Status.account_id == account.id,
        Status.visibility.in_(_OUTBOX_VISIBILITIES),
        Status.deleted_at.is_(None),
        Status.reblog_of_id.is_(None),
    )
    return int((await session.execute(stmt)).scalar_one())


async def _outbox_page_items(
    session, account: Account, *, page: int
) -> list[dict]:
    offset = max(0, (page - 1) * _PAGE_SIZE)
    stmt = (
        select(Status)
        .where(
            Status.account_id == account.id,
            Status.visibility.in_(_OUTBOX_VISIBILITIES),
            Status.deleted_at.is_(None),
            Status.reblog_of_id.is_(None),
        )
        .order_by(Status.id.desc())
        .offset(offset)
        .limit(_PAGE_SIZE)
    )
    rows = (await session.execute(stmt)).unique().scalars().all()
    return [serialize_create_activity(s, account) for s in rows]


@router.get("/users/{username}/outbox")
async def outbox_collection(
    username: str,
    session: DBSession,
    page: str | None = Query(default=None),
) -> Response:
    """OrderedCollection of Create activities wrapping this actor's
    PUBLIC and UNLISTED statuses. Excludes reblogs, soft-deleted, and
    non-public visibilities — peers don't get to see what they
    couldn't have received via direct delivery."""
    account = await _load_local_actor(session, username)
    endpoint = f"{_asset_host()}/users/{account.username}/outbox"
    if account.hide_collections:
        return Response(
            content=json.dumps(
                {
                    "@context": _AS2_CONTEXT,
                    "id": endpoint,
                    "type": "OrderedCollection",
                    "totalItems": 0,
                    "first": f"{endpoint}?page=1",
                }
            ),
            media_type=_AP_MEDIA_TYPE,
        )
    total = await _outbox_count(session, account)
    if page is None:
        body: dict = {
            "@context": _AS2_CONTEXT,
            "id": endpoint,
            "type": "OrderedCollection",
            "totalItems": total,
            "first": f"{endpoint}?page=1",
        }
    else:
        try:
            page_num = int(page)
        except ValueError:
            page_num = 1
        page_num = max(1, page_num)
        items = await _outbox_page_items(session, account, page=page_num)
        body = {
            "@context": _AS2_CONTEXT,
            "id": f"{endpoint}?page={page_num}",
            "type": "OrderedCollectionPage",
            "partOf": endpoint,
            "totalItems": total,
            "orderedItems": items,
        }
        if len(items) == _PAGE_SIZE:
            body["next"] = f"{endpoint}?page={page_num + 1}"
        if page_num > 1:
            body["prev"] = f"{endpoint}?page={page_num - 1}"
    return Response(content=json.dumps(body), media_type=_AP_MEDIA_TYPE)


@router.get("/users/{username}/featured")
async def featured_collection(username: str, session: DBSession) -> Response:
    """Pinned statuses as an OrderedCollection of Notes."""
    account = await _load_local_actor(session, username)
    endpoint = f"{_asset_host()}/users/{account.username}/featured"

    stmt = (
        select(Status)
        .join(StatusPin, StatusPin.status_id == Status.id)
        .where(StatusPin.account_id == account.id)
        .order_by(StatusPin.id.desc())
    )
    rows = (await session.execute(stmt)).unique().scalars().all()
    items = [serialize_note(s, account) for s in rows]

    body = {
        "@context": _AS2_CONTEXT,
        "id": endpoint,
        "type": "OrderedCollection",
        "totalItems": len(items),
        "orderedItems": items,
    }
    return Response(content=json.dumps(body), media_type=_AP_MEDIA_TYPE)


@router.get("/users/{username}/followers")
async def followers_collection(
    username: str,
    session: DBSession,
    page: str | None = Query(default=None),
) -> Response:
    return await _serve_collection(session, username, "followers", page)


@router.get("/users/{username}/following")
async def following_collection(
    username: str,
    session: DBSession,
    page: str | None = Query(default=None),
) -> Response:
    return await _serve_collection(session, username, "following", page)


@router.post(
    "/users/{username}/inbox", status_code=status.HTTP_202_ACCEPTED
)
async def actor_inbox(
    username: str,
    request: Request,
    session: DBSession,
    http_client: HttpClient,
    enqueuer: Enqueuer = Depends(get_enqueuer),
) -> Response:
    # The username path segment must resolve to a local actor — we
    # don't accept federated POSTs against URLs we don't own.
    row = (
        await session.execute(
            select(Account).where(
                Account.username == username, Account.domain.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Record not found"
        )
    body, actor_url = await _verify_or_401(request, session, http_client)
    await _dispatch_body(session, http_client, enqueuer, actor_url, body)
    return Response(status_code=status.HTTP_202_ACCEPTED)
