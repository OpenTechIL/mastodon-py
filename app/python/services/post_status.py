"""Status creation — the centerpiece of the write API.

What this slice covers:

  - Plain-text status with `visibility`, `sensitive`, `spoiler_text`,
    `language`.
  - Optional `in_reply_to_id` — looks up the parent (visibility-checked),
    sets `reply=True`, `in_reply_to_account_id=parent.account_id`.
  - Snowflake id (assigned in app code; falls back to DB default at
    insert time for cutover compatibility — both backends mint the same
    bit layout).
  - Canonical `uri` / `url` for local statuses.
  - `account_stats.statuses_count` increment and `last_status_at`
    timestamp move, via the explicit counter-cache helper.

Deferred to dedicated phases (each surfacing real product behavior we
just don't model yet):

  - **Media attachments / polls / quote / scheduled** — schema columns
    exist; the orchestration lives in the media pipeline, polls service,
    quote service, scheduler. Status creation here ignores those inputs.
  - **Mentions parsing** — would walk the text for `@user@domain` tokens,
    resolve accounts (creating remote stubs via webfinger), insert
    `mentions` rows, and notify recipients. The `mentions` table isn't
    yet modeled in Python.
  - **Hashtag extraction** — `Tag` / `statuses_tags` table not modeled.
  - **Link cards** — `FetchLinkCardWorker` enqueue.
  - **ActivityPub `Create` delivery + DistributionWorker** — federation
    and home-feed fan-out.
  - **`Idempotency-Key` header** — Mastodon's anti-double-post safety net.
    A future slice adds a Redis-backed `idempotency_keys:<account>:<key>`
    SETNX with TTL returning the cached status id.
  - **Antispam silent-drop** and **rate limiting**.
  - **Default visibility / default sensitive** from `users.settings`.
    Until that ports, callers must pass `visibility` explicitly or
    accept the PUBLIC default.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status as http_status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import asyncio

from app.python.common.counter_cache import adjust_counter
from app.python.common.snowflake import now_id
from app.python.federation.fanout import collect_inbox_urls
from app.python.federation.keys import ensure_local_actor_keys
from app.python.federation.serializers import serialize_create_activity
from app.python.lib import hashtags, mentions
from app.python.lib.asset_urls import _asset_host  # noqa: PLC2701 — same package
from app.python.models import (
    Account,
    AccountStat,
    Follow,
    Mention,
    NotificationType,
    Status,
    StatusStat,
    StatusTag,
    Tag,
    Visibility,
)
from app.python.policies.status_policy import visible_to
from app.python.queue import Enqueuer
from app.python.services.notifications import create_local as create_notification


_VISIBILITY_BY_NAME: dict[str, Visibility] = {
    "public": Visibility.PUBLIC,
    "unlisted": Visibility.UNLISTED,
    "private": Visibility.PRIVATE,
    "direct": Visibility.DIRECT,
}


class StatusValidationError(HTTPException):
    def __init__(self, detail: str) -> None:
        super().__init__(status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


async def post_status(
    session: AsyncSession,
    *,
    author: Account,
    text: str,
    visibility: str = "public",
    sensitive: bool | None = None,
    spoiler_text: str = "",
    language: str | None = None,
    in_reply_to_id: int | None = None,
    media_ids: list[int] | None = None,
    poll: "PollSpec | None" = None,
    enqueuer: Enqueuer | None = None,
) -> Status:
    text = text or ""
    spoiler_text = spoiler_text or ""
    media_ids = media_ids or []

    if not text.strip() and not spoiler_text.strip() and not media_ids and not poll:
        raise StatusValidationError("Text, media, or poll is required")

    if media_ids and poll:
        raise StatusValidationError("Status can't include both media and a poll")

    vis = _VISIBILITY_BY_NAME.get(visibility.lower())
    if vis is None:
        raise StatusValidationError(f"Invalid visibility: {visibility!r}")

    parent: Status | None = None
    if in_reply_to_id is not None:
        parent = (
            await session.execute(select(Status).where(Status.id == in_reply_to_id))
        ).scalar_one_or_none()
        if parent is None or not await visible_to(session, parent, author.id):
            raise StatusValidationError("Replying to a non-existent or invisible status")

    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    new_id = now_id()
    host = _asset_host()
    uri = f"{host}/users/{author.username}/statuses/{new_id}"
    url = f"{host}/@{author.username}/{new_id}"

    inferred_sensitive = (
        sensitive if sensitive is not None else bool(spoiler_text.strip())
    )
    row = Status(
        id=new_id,
        account_id=author.id,
        text=text,
        spoiler_text=spoiler_text,
        sensitive=inferred_sensitive,
        visibility=vis.value,
        language=language,
        local=True,
        reply=parent is not None,
        in_reply_to_id=parent.id if parent else None,
        in_reply_to_account_id=parent.account_id if parent else None,
        reblog_of_id=None,
        uri=uri,
        url=url,
        edited_at=None,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)

    # Initialize the per-status counter row so favourite/reblog/reply
    # bumps later don't have to special-case its absence.
    session.add(
        StatusStat(
            id=new_id,
            status_id=new_id,
            replies_count=0,
            reblogs_count=0,
            favourites_count=0,
            quotes_count=0,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()

    await adjust_counter(
        session,
        table="account_stats",
        row_id=author.id,
        column="statuses_count",
        delta=1,
    )
    # `last_status_at` isn't a counter — set it via the ORM so the bind
    # goes through the typed datetime adapter rather than SQLite's
    # deprecated default.
    await session.execute(
        update(AccountStat)
        .where(AccountStat.account_id == author.id)
        .values(last_status_at=now)
    )

    # Reply increments the parent's replies_count.
    if parent is not None:
        await adjust_counter(
            session,
            table="status_stats",
            row_id=parent.id,
            column="replies_count",
            delta=1,
        )

    await _link_hashtags(session, row, now=now)
    await _link_mentions(session, row, author=author, now=now)
    if media_ids:
        await _attach_media(session, status=row, author=author, media_ids=media_ids)
    if poll is not None:
        await _attach_poll(session, status=row, author=author, spec=poll, now=now)
    if vis == Visibility.DIRECT:
        await _attach_to_conversation(session, status=row, author=author, now=now)

    await session.commit()

    # Live streaming: publish to Redis so the streaming server pushes to subscribed clients.
    from app.python.services.streaming import publish_status  # noqa: PLC0415
    await publish_status(session, row, author)

    # Outbound federation: deliver to remote followers after commit.
    # DIRECT/LIMITED don't fan out via followers — mentions handle
    # those recipients, and mentions isn't wired in yet.
    if author.local and enqueuer is not None and vis is not Visibility.DIRECT and vis is not Visibility.LIMITED:
        await _enqueue_fanout(session, enqueuer, status=row, author=author)
    return row


async def _enqueue_fanout(
    session: AsyncSession,
    enqueuer: Enqueuer,
    *,
    status: Status,
    author: Account,
) -> None:
    """Collect remote-follower inboxes and enqueue one `deliver_activity`
    job carrying the Create.

    No-op when there are no remote followers — local-only audiences
    don't need federation. Done as a single job rather than per-recipient
    so asyncio inside the worker handles concurrency cheaply.

    Lazily backfills the author's RSA keypair when missing. Without
    keys the worker can't sign and drops the delivery silently — until
    the user-creation flow ports and generates them eagerly, generating
    them on first outbound is the only way to keep federation working.
    """
    rows = (
        await session.execute(
            select(Account)
            .join(Follow, Follow.account_id == Account.id)
            .where(
                Follow.target_account_id == author.id,
                Account.domain.is_not(None),
            )
        )
    ).unique().scalars().all()
    inbox_urls = collect_inbox_urls(rows)
    if not inbox_urls:
        return

    if not author.private_key:
        # RSA-2048 generation is ~200ms; punt to a worker thread so the
        # response thread isn't blocked. Mutates `author` in place; we
        # commit the new keys before enqueueing so the worker reading
        # the row finds them.
        await asyncio.to_thread(ensure_local_actor_keys, author)
        await session.commit()

    activity = serialize_create_activity(status, author)
    await enqueuer.enqueue("deliver_activity", activity, author.id, inbox_urls)


from dataclasses import dataclass
from datetime import timedelta


@dataclass(slots=True)
class PollSpec:
    """The bits `post_status` needs to materialise a Poll row.

    Defined as a tiny dataclass rather than reusing the pydantic
    `PollCreate` to keep the service module independent of the request-
    schema layer.
    """

    options: list[str]
    expires_in: int
    multiple: bool
    hide_totals: bool


async def _attach_poll(
    session: AsyncSession,
    *,
    status: Status,
    author: Account,
    spec: PollSpec,
    now: datetime,
) -> None:
    """Create a Poll attached to this Status. Validates options + expiry."""
    from app.python.models import Poll

    cleaned = [opt.strip() for opt in spec.options if opt.strip()]
    if len(cleaned) < 2:
        raise StatusValidationError("Polls must have at least two options")
    if len(cleaned) > 4:
        raise StatusValidationError("Polls can have at most four options")
    if len(set(cleaned)) != len(cleaned):
        raise StatusValidationError("Poll options must be unique")

    expires_at = now + timedelta(seconds=spec.expires_in)
    session.add(
        Poll(
            id=now_id(),
            account_id=author.id,
            status_id=status.id,
            options=cleaned,
            cached_tallies=[0] * len(cleaned),
            multiple=spec.multiple,
            hide_totals=spec.hide_totals,
            expires_at=expires_at,
            votes_count=0,
            voters_count=0 if spec.multiple else None,
            lock_version=0,
            created_at=now,
            updated_at=now,
        )
    )


async def _attach_media(
    session: AsyncSession,
    *,
    status: Status,
    author: Account,
    media_ids: list[int],
) -> None:
    """Bind unattached MediaAttachment rows to this status.

    Each id must belong to the author and not already be attached
    elsewhere. Up to 4 (matching `STATUS_MAX_MEDIA_ATTACHMENTS`). Invalid
    ids 422 the request — Mastodon's contract is that a status with
    bad media_ids should fail visibly rather than post text-only.
    """
    from sqlalchemy import select as sa_select
    from app.python.models import MediaAttachment

    if len(media_ids) > 4:
        raise StatusValidationError("Too many media attachments (max 4)")

    rows = (
        await session.execute(
            sa_select(MediaAttachment).where(MediaAttachment.id.in_(media_ids))
        )
    ).scalars().all()
    by_id = {row.id: row for row in rows}

    for mid in media_ids:
        att = by_id.get(mid)
        if att is None:
            raise StatusValidationError(f"Media attachment {mid} not found")
        if att.account_id != author.id:
            raise StatusValidationError(f"Media attachment {mid} not yours")
        if att.status_id is not None and att.status_id != status.id:
            raise StatusValidationError(
                f"Media attachment {mid} is already attached to another status"
            )
        att.status_id = status.id


async def _attach_to_conversation(
    session: AsyncSession,
    *,
    status: Status,
    author: Account,
    now: datetime,
) -> None:
    """For DIRECT-visibility posts: create/reuse a Conversation row and
    write AccountConversation rows for the author + every (local or
    remote) mentioned account.

    Local recipients also see this thread in `/api/v1/conversations`;
    remote recipients get the row so AP delivery can address them
    correctly when federation ports.
    """
    from app.python.services.conversations import (
        attach_to_conversation,
        mentioned_account_ids_for,
    )

    mentioned = await mentioned_account_ids_for(session, status.id)
    await attach_to_conversation(
        session,
        status=status,
        author_id=author.id,
        mentioned_account_ids=mentioned,
        now=now,
    )


async def _link_hashtags(
    session: AsyncSession, status: Status, *, now: datetime
) -> None:
    """Extract hashtags from `status.text`, upsert Tag rows, link via StatusTag.

    Each tag's `last_status_at` advances to the new status's timestamp,
    so the legacy admin "active tags" dashboard keeps working. Tag
    `display_name` is set on first creation only — preserves the
    casing of the first occurrence, matching Rails.
    """
    from sqlalchemy import select

    names = hashtags.extract(status.text)
    if not names:
        return

    existing = (
        await session.execute(select(Tag).where(Tag.name.in_(names)))
    ).scalars().all()
    by_name = {t.name: t for t in existing}

    tag_ids: list[int] = []
    for name in names:
        if name in by_name:
            tag = by_name[name]
            tag.last_status_at = now
            tag.updated_at = now
        else:
            tag = Tag(
                id=now_id(),
                name=name,
                display_name=name,
                usable=True,
                listable=True,
                trendable=None,
                last_status_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(tag)
            await session.flush()
        tag_ids.append(tag.id)

    for tag_id in tag_ids:
        session.add(StatusTag(tag_id=tag_id, status_id=status.id))


async def _link_mentions(
    session: AsyncSession,
    status: Status,
    *,
    author: Account,
    now: datetime,
) -> None:
    """Parse @-mentions, resolve to existing Accounts (skipping unknown
    remotes — webfinger lookup is deferred), insert Mention rows, and
    fire a `mention` notification per local recipient. Self-mentions
    don't get rows or notifications.
    """
    from sqlalchemy import and_, or_, select as sa_select

    parsed = mentions.extract(status.text)
    if not parsed:
        return

    predicates = []
    for username, domain in parsed:
        if domain is None:
            predicates.append(
                and_(Account.username.ilike(username), Account.domain.is_(None))
            )
        else:
            predicates.append(
                and_(
                    Account.username.ilike(username),
                    Account.domain.ilike(domain),
                )
            )
    accounts = (
        await session.execute(sa_select(Account).where(or_(*predicates)))
    ).scalars().all()

    for account in accounts:
        if account.id == author.id:
            continue
        mention = Mention(
            id=now_id(),
            account_id=account.id,
            status_id=status.id,
            silent=False,
            created_at=now,
            updated_at=now,
        )
        session.add(mention)
        await session.flush()
        # `activity_id` points at the Mention row (legacy contract);
        # the notifications router's `_resolve_statuses` walks
        # Mention.id → status_id to surface the post.
        await create_notification(
            session,
            recipient=account,
            actor=author,
            activity_id=mention.id,
            type=NotificationType.MENTION,
        )
