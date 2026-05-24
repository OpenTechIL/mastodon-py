"""Inbound ActivityPub activity dispatch.

`dispatch(session, actor_url, activity_json)` is the function the inbox
endpoint calls after signature verification. It looks at the activity's
`type` and routes to a handler that mutates DB state. Handlers are
**defensive**: every shape we don't recognize is a no-op, every
unresolvable reference (unknown remote actor, target not local) is a
no-op. We never raise — the inbox already returned 202, and surfacing
errors here would just retry-storm us.

The slice ships handlers for `Follow` and `Undo Follow`. Other types
(`Create`, `Like`, `Announce`, `Delete`, `Accept`, `Reject`) land in
follow-up slices alongside the activity-side logic they enable.

Activity shapes (abbreviated):

  Follow:
    { type: "Follow", id: "...", actor: "<remote>", object: "<local>" }

  Undo Follow:
    { type: "Undo", actor: "<remote>",
      object: { type: "Follow", actor: "<remote>", object: "<local>" } }
    or  object: "<follow-uri>"  (just the Follow's id)
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from sqlalchemy import delete, select

from app.python.common.counter_cache import adjust_counter
from app.python.common.snowflake import now_id
from app.python.federation.actor_fetcher import fetch_and_persist_actor
from app.python.federation.keys import ensure_local_actor_keys
from app.python.lib.asset_urls import account_uri
from app.python.models import (
    Account,
    Block,
    Favourite,
    Follow,
    FollowRequest,
    Status,
    Visibility,
)

# Activity-level dedup: the AP spec says servers MUST NOT process an
# activity with the same `id` more than once. We keep the last 10 000
# seen activity IDs in memory. This is per-process, so a restart clears
# it — that's acceptable because the underlying handlers are all
# idempotent at the DB level. Using an OrderedDict as a bounded LRU set.
_SEEN_ACTIVITY_IDS: OrderedDict[str, None] = OrderedDict()
_SEEN_MAX = 10_000


def clear_activity_dedup_cache() -> None:
    """Empty the in-process dedup set. Intended for use in tests only."""
    _SEEN_ACTIVITY_IDS.clear()


if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.python.queue import Enqueuer


async def dispatch(
    *,
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    enqueuer: Enqueuer | None,
    actor_url: str,
    activity: dict[str, Any],
) -> None:
    """Route the verified activity to its handler. Defensive on shape.

    `http_client` is used to fetch the actor JSON on first contact —
    the dispatcher auto-creates an Account stub for previously-unknown
    remote actors. Handlers don't need to worry about whether the
    actor exists.

    `enqueuer` carries the outbound side: a Follow auto-accepts (sends
    Accept back), Like/Announce on local statuses fan out to the
    author's other followers, etc. Optional because some handlers
    don't need it; tests can pass None for inbound-only checks.
    """
    # Activity-level deduplication: spec requires MUST NOT process the
    # same activity id twice. Check before any DB work.
    activity_id = activity.get("id")
    if isinstance(activity_id, str) and activity_id:
        if activity_id in _SEEN_ACTIVITY_IDS:
            return
        _SEEN_ACTIVITY_IDS[activity_id] = None
        if len(_SEEN_ACTIVITY_IDS) > _SEEN_MAX:
            _SEEN_ACTIVITY_IDS.popitem(last=False)

    # Ensure the verified actor is in the DB before any handler runs.
    # Handlers downstream call `_resolve_remote_actor(session, …)`
    # which is a pure-DB lookup; this step fills the gap.
    await fetch_and_persist_actor(session, http_client, actor_url)
    await _route_activity(session=session, enqueuer=enqueuer, actor_url=actor_url, activity=activity)


async def _route_undo(
    session: AsyncSession,
    actor_url: str,
    inner: Any,
) -> None:
    if isinstance(inner, dict):
        inner_type = inner.get("type")
        if inner_type == "Follow":
            await _handle_undo_follow(session, actor_url, inner)
        elif inner_type == "Like":
            await _handle_undo_like(session, actor_url, inner)
        elif inner_type == "Announce":
            await _handle_undo_announce(session, actor_url, inner)
    elif isinstance(inner, str):
        # URI-only Undo: we don't know which kind without fetching
        # the original. Best-effort: try each kind of row, scoped
        # to the verified actor so we can't undo someone else's.
        await _handle_undo_follow_by_uri(session, actor_url, inner)
        await _handle_undo_announce_by_uri(session, actor_url, inner)


async def _route_activity(
    *,
    session: AsyncSession,
    enqueuer: Enqueuer | None,
    actor_url: str,
    activity: dict[str, Any],
) -> None:
    activity_type = activity.get("type")
    if activity_type == "Follow":
        await _handle_follow(session, actor_url, activity, enqueuer)
    elif activity_type == "Undo":
        await _route_undo(session, actor_url, activity.get("object"))
    elif activity_type == "Create":
        inner = activity.get("object")
        if isinstance(inner, dict) and inner.get("type") in {"Note", "Article"}:
            await _handle_create_note(session, actor_url, inner)
    elif activity_type == "Like":
        await _handle_like(session, actor_url, activity)
    elif activity_type == "Announce":
        await _handle_announce(session, actor_url, activity)
    elif activity_type == "Delete":
        target = activity.get("object")
        # `object` can be the deleted Tombstone, or just its URI.
        target_uri = target.get("id") if isinstance(target, dict) else target
        if isinstance(target_uri, str):
            await _handle_delete_status(session, actor_url, target_uri)
    elif activity_type == "Accept":
        inner = activity.get("object")
        if isinstance(inner, dict) and inner.get("type") == "Follow":
            await _handle_accept_follow(session, actor_url, inner)
        elif isinstance(inner, str):
            # URI-only Accept — look up the pending FollowRequest by uri
            await _handle_accept_follow_by_uri(session, actor_url, inner)
    elif activity_type == "Reject":
        inner = activity.get("object")
        if isinstance(inner, dict) and inner.get("type") == "Follow":
            await _handle_reject_follow(session, actor_url, inner)
        elif isinstance(inner, str):
            await _handle_reject_follow_by_uri(session, actor_url, inner)
    elif activity_type == "Update":
        inner = activity.get("object")
        if isinstance(inner, dict):
            actor_types = {"Person", "Service", "Application", "Group", "Organization"}
            if inner.get("type") in actor_types:
                await _handle_update_actor(session, actor_url, inner)
            elif inner.get("type") in {"Note", "Article"}:
                await _handle_update_note(session, actor_url, inner)
    elif activity_type == "Block":
        await _handle_block(session, actor_url, activity)
    # Every other type is a no-op for this slice.


async def _authorize_follow_request(session: AsyncSession, request: FollowRequest) -> None:
    """Convert a pending FollowRequest to a Follow (accept path)."""
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    session.add(
        Follow(
            id=now_id(),
            account_id=request.account_id,
            target_account_id=request.target_account_id,
            uri=request.uri,
            show_reblogs=request.show_reblogs,
            notify=request.notify,
            languages=request.languages,
            created_at=now,
            updated_at=now,
        )
    )
    await session.execute(delete(FollowRequest).where(FollowRequest.id == request.id))
    await adjust_counter(session, table="account_stats", row_id=request.account_id, column="following_count", delta=1)
    await adjust_counter(
        session, table="account_stats", row_id=request.target_account_id, column="followers_count", delta=1
    )
    await session.commit()


async def _handle_accept_follow(
    session: AsyncSession,
    actor_url: str,
    follow_obj: dict[str, Any],
) -> None:
    """Remote locked account accepted our local user's Follow.

    actor = the remote account being followed (the one who sent Accept).
    object = the embedded Follow activity that was originally sent by us.
    The `actor` field on the inner Follow should be our local user's URI.
    """
    remote_account = await _resolve_remote_actor(session, actor_url)
    if remote_account is None:
        return
    local_actor_uri = follow_obj.get("actor")
    if not isinstance(local_actor_uri, str):
        return
    local_account = await _resolve_local_target(session, local_actor_uri)
    if local_account is None:
        return

    request = (
        await session.execute(
            select(FollowRequest).where(
                FollowRequest.account_id == local_account.id,
                FollowRequest.target_account_id == remote_account.id,
            )
        )
    ).scalar_one_or_none()
    if request is None:
        return
    await _authorize_follow_request(session, request)


async def _handle_accept_follow_by_uri(
    session: AsyncSession,
    actor_url: str,
    follow_uri: str,
) -> None:
    """Accept where the inner object is just the Follow URI string."""
    remote_account = await _resolve_remote_actor(session, actor_url)
    if remote_account is None:
        return
    request = (
        await session.execute(
            select(FollowRequest).where(
                FollowRequest.uri == follow_uri,
                FollowRequest.target_account_id == remote_account.id,
            )
        )
    ).scalar_one_or_none()
    if request is None:
        return
    await _authorize_follow_request(session, request)


async def _handle_reject_follow(
    session: AsyncSession,
    actor_url: str,
    follow_obj: dict[str, Any],
) -> None:
    """Remote locked account rejected our local user's Follow request."""
    remote_account = await _resolve_remote_actor(session, actor_url)
    if remote_account is None:
        return
    local_actor_uri = follow_obj.get("actor")
    if not isinstance(local_actor_uri, str):
        return
    local_account = await _resolve_local_target(session, local_actor_uri)
    if local_account is None:
        return

    await session.execute(
        delete(FollowRequest).where(
            FollowRequest.account_id == local_account.id,
            FollowRequest.target_account_id == remote_account.id,
        )
    )
    # Also remove any already-promoted Follow (edge case: race with authorize)
    await session.execute(
        delete(Follow).where(
            Follow.account_id == local_account.id,
            Follow.target_account_id == remote_account.id,
        )
    )
    await session.commit()


async def _handle_reject_follow_by_uri(
    session: AsyncSession,
    actor_url: str,
    follow_uri: str,
) -> None:
    """Reject where the inner object is just the Follow URI string."""
    remote_account = await _resolve_remote_actor(session, actor_url)
    if remote_account is None:
        return
    await session.execute(
        delete(FollowRequest).where(
            FollowRequest.uri == follow_uri,
            FollowRequest.target_account_id == remote_account.id,
        )
    )
    await session.commit()


async def _resolve_local_target(session: AsyncSession, object_field: Any) -> Account | None:
    """`object` in a Follow is the target actor URL.

    Returns the local Account it points at, or None if the URL doesn't
    map to a local user we host (path not `/users/<name>`, username
    unknown, or the row is a remote actor sharing a username).
    """
    if not isinstance(object_field, str):
        return None
    parsed = urlparse(object_field)
    if parsed.scheme not in {"http", "https"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    # We accept `/users/<name>` — the canonical local actor URL shape.
    if len(parts) < 2 or parts[0] != "users":
        return None
    username = parts[1]
    return (
        await session.execute(select(Account).where(Account.username == username, Account.domain.is_(None)))
    ).scalar_one_or_none()


async def _resolve_remote_actor(session: AsyncSession, actor_url: str) -> Account | None:
    """Look up the verified remote actor by URI.

    For this slice we don't auto-create stubs for never-seen actors —
    that's `FetchRemoteAccountService`'s job and lands separately.
    """
    return (await session.execute(select(Account).where(Account.uri == actor_url))).scalar_one_or_none()


async def _handle_follow(
    session: AsyncSession,
    actor_url: str,
    activity: dict[str, Any],
    enqueuer: Enqueuer | None = None,
) -> None:
    follower = await _resolve_remote_actor(session, actor_url)
    target = await _resolve_local_target(session, activity.get("object"))
    if follower is None or target is None:
        return
    if follower.id == target.id:
        return  # self-follows aren't a thing

    follow_uri = activity.get("id") if isinstance(activity.get("id"), str) else None

    if target.locked:
        # Locked target → FollowRequest until the user accepts. Idempotent
        # on (account_id, target_account_id). No Accept emitted here —
        # the authorize endpoint sends it when the user clicks through.
        existing_req = (
            await session.execute(
                select(FollowRequest).where(
                    FollowRequest.account_id == follower.id,
                    FollowRequest.target_account_id == target.id,
                )
            )
        ).scalar_one_or_none()
        if existing_req is not None:
            existing_req.uri = follow_uri or existing_req.uri
            await session.commit()
            return
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        session.add(
            FollowRequest(
                id=now_id(),
                account_id=follower.id,
                target_account_id=target.id,
                uri=follow_uri,
                show_reblogs=True,
                notify=False,
                languages=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
        return

    # Unlocked target → Follow directly. Idempotent on (account_id,
    # target_account_id).
    existing = (
        await session.execute(
            select(Follow).where(
                Follow.account_id == follower.id,
                Follow.target_account_id == target.id,
            )
        )
    ).scalar_one_or_none()
    is_new = existing is None
    if existing is not None:
        existing.uri = follow_uri or existing.uri
        await session.commit()
    else:
        now = datetime.now(tz=UTC).replace(tzinfo=None)
        session.add(
            Follow(
                id=now_id(),
                account_id=follower.id,
                target_account_id=target.id,
                uri=follow_uri,
                show_reblogs=True,
                notify=False,
                languages=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    # Send Accept back so the follower's side moves out of pending.
    # Only on first-creation: redeliveries of the same Follow shouldn't
    # spam Accepts.
    if is_new and enqueuer is not None:
        await _send_accept_for_follow(session, enqueuer, target=target, follower=follower, follow=activity)


async def _send_accept_for_follow(
    session: AsyncSession,
    enqueuer: Enqueuer,
    *,
    target: Account,
    follower: Account,
    follow: dict[str, Any],
) -> None:
    """Emit an Accept activity to the follower's inbox.

    Lazy-ensures the local target has a keypair before enqueuing —
    `deliver_activity` would silently drop with no key to sign.
    Same backfill pattern as `post_status._enqueue_fanout`.
    """
    inbox_url = (follower.shared_inbox_url or "").strip() or (follower.inbox_url or "").strip()
    if not inbox_url:
        return  # nowhere to deliver

    if not target.private_key:
        await asyncio.to_thread(ensure_local_actor_keys, target)
        await session.commit()

    target_uri = account_uri(target)
    accept = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{target_uri}#accepts/{follow.get('id', '')}",
        "type": "Accept",
        "actor": target_uri,
        "object": follow,
    }
    await enqueuer.enqueue("deliver_activity", accept, target.id, [inbox_url])


async def _handle_undo_follow(
    session: AsyncSession,
    actor_url: str,
    follow_activity: dict[str, Any],
) -> None:
    follower = await _resolve_remote_actor(session, actor_url)
    target = await _resolve_local_target(session, follow_activity.get("object"))
    if follower is None or target is None:
        return
    await session.execute(
        delete(Follow).where(
            Follow.account_id == follower.id,
            Follow.target_account_id == target.id,
        )
    )
    await session.execute(
        delete(FollowRequest).where(
            FollowRequest.account_id == follower.id,
            FollowRequest.target_account_id == target.id,
        )
    )
    await session.commit()


async def _handle_undo_follow_by_uri(
    session: AsyncSession,
    actor_url: str,
    follow_uri: str,
) -> None:
    """Undo where the inner `object` is just the Follow's id (string).

    We can still find the row because we stored `uri` on it at follow
    time. Restrict the delete to rows whose account belongs to the
    actor — defense against an actor undoing someone else's follow.
    """
    follower = await _resolve_remote_actor(session, actor_url)
    if follower is None:
        return
    await session.execute(
        delete(Follow).where(
            Follow.uri == follow_uri,
            Follow.account_id == follower.id,
        )
    )
    await session.execute(
        delete(FollowRequest).where(
            FollowRequest.uri == follow_uri,
            FollowRequest.account_id == follower.id,
        )
    )
    await session.commit()


_AS2_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def _as_list(value: Any) -> list[Any]:
    """AP audience fields (`to`, `cc`) are list-or-scalar. Normalize."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _derive_visibility(to: list[Any], cc: list[Any], followers_url: str | None) -> Visibility:
    """Map AP `to`/`cc` to Mastodon's visibility enum.

    Rules (Mastodon-compatible):
      - PUBLIC: to contains as:Public
      - UNLISTED: cc contains as:Public, to doesn't
      - PRIVATE: to contains the author's followers collection
      - DIRECT: anything else (default; explicit user URLs in to/cc)
    """
    to_set = {v for v in to if isinstance(v, str)}
    cc_set = {v for v in cc if isinstance(v, str)}
    if _AS2_PUBLIC in to_set:
        return Visibility.PUBLIC
    if _AS2_PUBLIC in cc_set:
        return Visibility.UNLISTED
    if followers_url and followers_url in (to_set | cc_set):
        return Visibility.PRIVATE
    return Visibility.DIRECT


async def _handle_create_note(
    session: AsyncSession,
    actor_url: str,
    note: dict[str, Any],
) -> None:
    """Materialize an inbound AP Note as a Status row.

    Minimum viable port: visibility, text, spoiler, sensitive, uri, url,
    language, in_reply_to_id (only if we already have that parent). Skip
    mentions/attachments/polls/replies-resolution for now — handlers
    for those land alongside the corresponding inbound-side ports.

    Idempotent on `Status.uri` — re-deliveries (or duplicate fan-outs
    from a peer that didn't see our 202) become no-ops.
    """
    author = await _resolve_remote_actor(session, actor_url)
    if author is None:
        return

    # AP allows `attributedTo` to disagree with the activity's actor;
    # Mastodon's contract is to drop those — the signature was the
    # actor's, not the claimed author's.
    attributed_to = note.get("attributedTo")
    if isinstance(attributed_to, str) and attributed_to != actor_url:
        return

    uri = note.get("id")
    if not isinstance(uri, str) or not uri:
        return

    # Idempotency: skip if we already have this Status.
    existing = (await session.execute(select(Status).where(Status.uri == uri))).scalar_one_or_none()
    if existing is not None:
        return

    content = note.get("content") if isinstance(note.get("content"), str) else ""
    summary = note.get("summary") if isinstance(note.get("summary"), str) else ""
    sensitive = bool(note.get("sensitive", False))
    language = note.get("contentMap")
    # `contentMap` is `{lang: html}`; take the first key as the language tag.
    language_tag: str | None = None
    if isinstance(language, dict) and language:
        first = next(iter(language.keys()))
        if isinstance(first, str):
            language_tag = first

    url_field = note.get("url")
    page_url: str | None = url_field if isinstance(url_field, str) else None

    to = _as_list(note.get("to"))
    cc = _as_list(note.get("cc"))
    # Followers URL: prefer what the author advertises, fall back to
    # the canonical `/followers` path on their actor URI.
    followers_url = f"{actor_url}/followers"
    visibility = _derive_visibility(to, cc, followers_url)

    in_reply_to_uri = note.get("inReplyTo")
    in_reply_to_id: int | None = None
    in_reply_to_account_id: int | None = None
    if isinstance(in_reply_to_uri, str) and in_reply_to_uri:
        parent = (await session.execute(select(Status).where(Status.uri == in_reply_to_uri))).scalar_one_or_none()
        if parent is not None:
            in_reply_to_id = parent.id
            in_reply_to_account_id = parent.account_id

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    session.add(
        Status(
            id=now_id(),
            account_id=author.id,
            text=content,
            spoiler_text=summary,
            sensitive=sensitive,
            visibility=visibility.value,
            language=language_tag,
            local=False,
            reply=in_reply_to_id is not None,
            in_reply_to_id=in_reply_to_id,
            in_reply_to_account_id=in_reply_to_account_id,
            reblog_of_id=None,
            uri=uri,
            url=page_url,
            edited_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()


async def _handle_delete_status(
    session: AsyncSession,
    actor_url: str,
    target_uri: str,
) -> None:
    """Soft-delete a status the verified actor authored.

    Restricts the update to rows whose author is the verified actor —
    can't delete someone else's post even if a peer tells us to.
    """
    author = await _resolve_remote_actor(session, actor_url)
    if author is None:
        return
    row = (
        await session.execute(
            select(Status).where(
                Status.uri == target_uri,
                Status.account_id == author.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    row.deleted_at = datetime.now(tz=UTC).replace(tzinfo=None)
    await session.commit()


def _object_uri(value: Any) -> str | None:
    """`object` may be a URI string or a nested dict with `id`."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        inner = value.get("id")
        if isinstance(inner, str):
            return inner
    return None


async def _resolve_status_by_uri(session: AsyncSession, uri: str) -> Status | None:
    return (
        await session.execute(select(Status).where(Status.uri == uri, Status.deleted_at.is_(None)))
    ).scalar_one_or_none()


async def _handle_like(
    session: AsyncSession,
    actor_url: str,
    activity: dict[str, Any],
) -> None:
    """Inbound favourite. Idempotent on (account_id, status_id).

    Bumps `status_stats.favourites_count` via the counter helper so
    Postgres concurrency stays correct under fan-out from many peers
    hitting the same popular toot.
    """
    actor = await _resolve_remote_actor(session, actor_url)
    if actor is None:
        return
    target_uri = _object_uri(activity.get("object"))
    if not target_uri:
        return
    target = await _resolve_status_by_uri(session, target_uri)
    if target is None:
        return

    existing = (
        await session.execute(
            select(Favourite).where(
                Favourite.account_id == actor.id,
                Favourite.status_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    session.add(
        Favourite(
            id=now_id(),
            account_id=actor.id,
            status_id=target.id,
            created_at=now,
            updated_at=now,
        )
    )
    await adjust_counter(
        session,
        table="status_stats",
        row_id=target.id,
        column="favourites_count",
        delta=1,
    )
    await session.commit()


async def _handle_undo_like(
    session: AsyncSession,
    actor_url: str,
    like_activity: dict[str, Any],
) -> None:
    actor = await _resolve_remote_actor(session, actor_url)
    if actor is None:
        return
    target_uri = _object_uri(like_activity.get("object"))
    if not target_uri:
        return
    target = await _resolve_status_by_uri(session, target_uri)
    if target is None:
        return

    result = await session.execute(
        delete(Favourite).where(
            Favourite.account_id == actor.id,
            Favourite.status_id == target.id,
        )
    )
    if result.rowcount:  # type: ignore[attr-defined]
        await adjust_counter(
            session,
            table="status_stats",
            row_id=target.id,
            column="favourites_count",
            delta=-1,
        )
    await session.commit()


async def _handle_announce(
    session: AsyncSession,
    actor_url: str,
    activity: dict[str, Any],
) -> None:
    """Inbound boost. Creates a reblog Status row referencing the target.

    Idempotent on (account_id, reblog_of_id) — peers occasionally
    re-deliver, and Mastodon's UI relies on at-most-one boost per
    (actor, status). Bumps `status_stats.reblogs_count` on the
    original.
    """
    actor = await _resolve_remote_actor(session, actor_url)
    if actor is None:
        return
    target_uri = _object_uri(activity.get("object"))
    if not target_uri:
        return
    target = await _resolve_status_by_uri(session, target_uri)
    if target is None:
        return

    existing = (
        await session.execute(
            select(Status).where(
                Status.account_id == actor.id,
                Status.reblog_of_id == target.id,
                Status.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    announce_uri = activity.get("id") if isinstance(activity.get("id"), str) else None
    to = _as_list(activity.get("to"))
    cc = _as_list(activity.get("cc"))
    visibility = _derive_visibility(to, cc, f"{actor_url}/followers")

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    session.add(
        Status(
            id=now_id(),
            account_id=actor.id,
            text="",
            spoiler_text="",
            sensitive=False,
            visibility=visibility.value,
            language=None,
            local=False,
            reply=False,
            in_reply_to_id=None,
            in_reply_to_account_id=None,
            reblog_of_id=target.id,
            uri=announce_uri,
            url=None,
            edited_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    await adjust_counter(
        session,
        table="status_stats",
        row_id=target.id,
        column="reblogs_count",
        delta=1,
    )
    await session.commit()


async def _handle_undo_announce(
    session: AsyncSession,
    actor_url: str,
    announce_activity: dict[str, Any],
) -> None:
    actor = await _resolve_remote_actor(session, actor_url)
    if actor is None:
        return
    target_uri = _object_uri(announce_activity.get("object"))
    if not target_uri:
        return
    target = await _resolve_status_by_uri(session, target_uri)
    if target is None:
        return

    existing = (
        await session.execute(
            select(Status).where(
                Status.account_id == actor.id,
                Status.reblog_of_id == target.id,
                Status.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return
    existing.deleted_at = datetime.now(tz=UTC).replace(tzinfo=None)
    await adjust_counter(
        session,
        table="status_stats",
        row_id=target.id,
        column="reblogs_count",
        delta=-1,
    )
    await session.commit()


async def _handle_update_actor(
    session: AsyncSession,
    actor_url: str,
    actor: dict[str, Any],
) -> None:
    """Apply a remote actor's profile update to our cached Account row.

    Restricts to the verified actor — peers can only update themselves.
    `id` on the inner object must match the activity's actor; mismatch
    means someone's trying to update a different account using their
    own signature, which we drop.
    """
    target_uri = actor.get("id")
    if not isinstance(target_uri, str) or target_uri != actor_url:
        return
    row = await _resolve_remote_actor(session, actor_url)
    if row is None:
        return

    # Each field that's actually present in the payload gets applied;
    # absent fields keep the cached value (peers don't always send the
    # full Person every time).
    if isinstance(actor.get("name"), str):
        row.display_name = actor["name"]
    if isinstance(actor.get("summary"), str):
        row.note = actor["summary"]
    if isinstance(actor.get("url"), str):
        row.url = actor["url"]
    if isinstance(actor.get("manuallyApprovesFollowers"), bool):
        row.locked = actor["manuallyApprovesFollowers"]
    if isinstance(actor.get("discoverable"), bool):
        row.discoverable = actor["discoverable"]
    if isinstance(actor.get("indexable"), bool):
        row.indexable = actor["indexable"]
    if isinstance(actor.get("type"), str):
        row.actor_type = actor["type"]

    # Public key rotation: peers occasionally roll their keys. Picking
    # this up means our signature verification keeps working after the
    # rotation — and a stale key would silently 401 every inbox POST.
    new_pem = _extract_pem(actor.get("publicKey"))
    if new_pem is not None:
        row.public_key = new_pem

    new_inbox = actor.get("inbox")
    if isinstance(new_inbox, str):
        row.inbox_url = new_inbox
    new_shared = _shared_inbox(actor)
    if new_shared:
        row.shared_inbox_url = new_shared

    row.updated_at = datetime.now(tz=UTC).replace(tzinfo=None)
    await session.commit()


def _extract_pem(public_key: Any) -> str | None:
    """`publicKey` can be a single object or a list (key rotation overlap)."""
    if isinstance(public_key, list):
        public_key = next((p for p in public_key if isinstance(p, dict)), None)
    if not isinstance(public_key, dict):
        return None
    pem = public_key.get("publicKeyPem")
    if isinstance(pem, str) and "BEGIN PUBLIC KEY" in pem:
        return pem
    return None


def _shared_inbox(actor: dict[str, Any]) -> str:
    endpoints = actor.get("endpoints")
    if isinstance(endpoints, dict):
        si = endpoints.get("sharedInbox")
        if isinstance(si, str):
            return si
    return ""


async def _handle_update_note(
    session: AsyncSession,
    actor_url: str,
    note: dict[str, Any],
) -> None:
    """Apply a remote status edit (Update wrapping a Note).

    Finds the existing Status by URI, updates the mutable fields, and
    stamps `edited_at`. Restricted to the verified actor's own content.
    We don't store edit history rows (status_edits table) — that's a
    future port — but we do update the live text so the current content
    stays accurate.
    """
    author = await _resolve_remote_actor(session, actor_url)
    if author is None:
        return

    uri = note.get("id")
    if not isinstance(uri, str) or not uri:
        return

    attributed_to = note.get("attributedTo")
    if isinstance(attributed_to, str) and attributed_to != actor_url:
        return

    row = (
        await session.execute(
            select(Status).where(
                Status.uri == uri,
                Status.account_id == author.id,
                Status.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return

    if isinstance(note.get("content"), str):
        row.text = note["content"]
    if isinstance(note.get("summary"), str):
        row.spoiler_text = note["summary"]
    if isinstance(note.get("sensitive"), bool):
        row.sensitive = note["sensitive"]

    content_map = note.get("contentMap")
    if isinstance(content_map, dict) and content_map:
        first = next(iter(content_map.keys()), None)
        if isinstance(first, str):
            row.language = first

    row.edited_at = datetime.now(tz=UTC).replace(tzinfo=None)
    row.updated_at = row.edited_at
    await session.commit()


async def _handle_block(
    session: AsyncSession,
    actor_url: str,
    activity: dict[str, Any],
) -> None:
    """Record an inbound Block from a remote actor targeting a local user.

    We store it in the `blocks` table so that follow-state cleanup and
    federation filtering can reference it. Also tears down any existing
    Follow / FollowRequest in either direction between the pair, which
    is the expected Mastodon behaviour on receiving a remote Block.
    """
    blocker = await _resolve_remote_actor(session, actor_url)
    if blocker is None:
        return

    target = await _resolve_local_target(session, activity.get("object"))
    if target is None:
        return

    existing = (
        await session.execute(
            select(Block).where(
                Block.account_id == blocker.id,
                Block.target_account_id == target.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return  # idempotent

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    block_uri = activity.get("id") if isinstance(activity.get("id"), str) else None
    session.add(
        Block(
            id=now_id(),
            account_id=blocker.id,
            target_account_id=target.id,
            uri=block_uri,
            created_at=now,
            updated_at=now,
        )
    )

    # Tear down follow relationships in both directions
    for follower_id, followed_id in [
        (blocker.id, target.id),
        (target.id, blocker.id),
    ]:
        await session.execute(
            delete(Follow).where(
                Follow.account_id == follower_id,
                Follow.target_account_id == followed_id,
            )
        )
        await session.execute(
            delete(FollowRequest).where(
                FollowRequest.account_id == follower_id,
                FollowRequest.target_account_id == followed_id,
            )
        )

    await session.commit()


async def _handle_undo_announce_by_uri(
    session: AsyncSession,
    actor_url: str,
    announce_uri: str,
) -> None:
    """URI-only Undo for an Announce — we look the reblog up by its
    own uri rather than the target uri."""
    actor = await _resolve_remote_actor(session, actor_url)
    if actor is None:
        return
    reblog = (
        await session.execute(
            select(Status).where(
                Status.uri == announce_uri,
                Status.account_id == actor.id,
                Status.reblog_of_id.is_not(None),
                Status.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if reblog is None:
        return
    target_id = reblog.reblog_of_id
    reblog.deleted_at = datetime.now(tz=UTC).replace(tzinfo=None)
    if target_id is not None:
        await adjust_counter(
            session,
            table="status_stats",
            row_id=target_id,
            column="reblogs_count",
            delta=-1,
        )
    await session.commit()
