"""Batch viewer-relationship lookup + mention list for status responses.

The legacy backend builds a `StatusRelationshipsPresenter` ahead of
serialization so 20 statuses on a timeline don't trigger 20×N queries.
We do the same: one IN-clause query per relationship table, keyed on
status_id, folded into a dict the serializer reads.

Mentions aren't viewer-specific — they're rendered the same for everyone
— but the same batch loader is the natural place to materialize them:
already walking the status_ids list once per response.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.models import (
    Account,
    Bookmark,
    Favourite,
    MediaAttachment,
    Mention,
    Poll,
    PollVote,
    Status,
    StatusPin,
)


@dataclass(slots=True)
class MentionEntry:
    """One mention rendered onto a status's `mentions` array."""

    id: str
    username: str
    acct: str
    url: str


@dataclass(slots=True)
class StatusRelationships:
    """Viewer-specific flags + per-status mention/media list for a batch."""

    favourited_ids: set[int] = field(default_factory=set)
    bookmarked_ids: set[int] = field(default_factory=set)
    reblogged_ids: set[int] = field(default_factory=set)
    pinned_ids: set[int] = field(default_factory=set)
    mentions_by_status: dict[int, list[MentionEntry]] = field(default_factory=dict)
    media_by_status: dict[int, list[MediaAttachment]] = field(default_factory=dict)
    poll_by_status: dict[int, Poll] = field(default_factory=dict)
    own_poll_votes: dict[int, list[int]] = field(default_factory=dict)

    def for_status(self, status_id: int) -> dict[str, bool]:
        return {
            "favourited": status_id in self.favourited_ids,
            "bookmarked": status_id in self.bookmarked_ids,
            "reblogged": status_id in self.reblogged_ids,
            "pinned": status_id in self.pinned_ids,
        }

    def mentions_for(self, status_id: int) -> list[MentionEntry]:
        return self.mentions_by_status.get(status_id, [])

    def media_for(self, status_id: int) -> list[MediaAttachment]:
        return self.media_by_status.get(status_id, [])

    def poll_for(self, status_id: int) -> Poll | None:
        return self.poll_by_status.get(status_id)

    def own_votes_for(self, poll_id: int) -> list[int]:
        return self.own_poll_votes.get(poll_id, [])


async def load_relationships(
    session: AsyncSession,
    viewer_account_id: int | None,
    status_ids: Iterable[int],
) -> StatusRelationships:
    ids = list(status_ids)
    mentions_by_status = await _load_mentions(session, ids)
    media_by_status = await _load_media(session, ids)
    poll_by_status, poll_ids = await _load_polls(session, ids)
    own_poll_votes = await _load_own_poll_votes(session, poll_ids, viewer_account_id)

    if not ids or viewer_account_id is None:
        return StatusRelationships(
            mentions_by_status=mentions_by_status,
            media_by_status=media_by_status,
            poll_by_status=poll_by_status,
            own_poll_votes=own_poll_votes,
        )

    favs = (
        (
            await session.execute(
                select(Favourite.status_id).where(
                    Favourite.account_id == viewer_account_id,
                    Favourite.status_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    marks = (
        (
            await session.execute(
                select(Bookmark.status_id).where(
                    Bookmark.account_id == viewer_account_id,
                    Bookmark.status_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    rebs = (
        (
            await session.execute(
                select(Status.reblog_of_id).where(
                    Status.account_id == viewer_account_id,
                    Status.reblog_of_id.in_(ids),
                    Status.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    pins = (
        (
            await session.execute(
                select(StatusPin.status_id).where(
                    StatusPin.account_id == viewer_account_id,
                    StatusPin.status_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return StatusRelationships(
        favourited_ids=set(favs),
        bookmarked_ids=set(marks),
        reblogged_ids={r for r in rebs if r is not None},
        pinned_ids=set(pins),
        mentions_by_status=mentions_by_status,
        media_by_status=media_by_status,
        poll_by_status=poll_by_status,
        own_poll_votes=own_poll_votes,
    )


async def _load_polls(session: AsyncSession, status_ids: list[int]) -> tuple[dict[int, Poll], list[int]]:
    if not status_ids:
        return {}, []
    rows = (await session.execute(select(Poll).where(Poll.status_id.in_(status_ids)))).scalars().all()
    out = {row.status_id: row for row in rows}
    return out, [row.id for row in rows]


async def _load_own_poll_votes(
    session: AsyncSession,
    poll_ids: list[int],
    viewer_account_id: int | None,
) -> dict[int, list[int]]:
    if not poll_ids or viewer_account_id is None:
        return {}
    rows = (
        await session.execute(
            select(PollVote.poll_id, PollVote.choice).where(
                PollVote.poll_id.in_(poll_ids),
                PollVote.account_id == viewer_account_id,
            )
        )
    ).all()
    out: dict[int, list[int]] = {}
    for pid, choice in rows:
        out.setdefault(pid, []).append(choice)
    for pid in out:
        out[pid].sort()
    return out


async def _load_media(session: AsyncSession, status_ids: list[int]) -> dict[int, list[MediaAttachment]]:
    if not status_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(MediaAttachment)
                .where(MediaAttachment.status_id.in_(status_ids))
                .order_by(MediaAttachment.id.asc())
            )
        )
        .scalars()
        .all()
    )
    out: dict[int, list[MediaAttachment]] = {}
    for row in rows:
        if row.status_id is None:
            continue
        out.setdefault(row.status_id, []).append(row)
    return out


async def _load_mentions(session: AsyncSession, status_ids: list[int]) -> dict[int, list[MentionEntry]]:
    if not status_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Mention.status_id,
                Account.id,
                Account.username,
                Account.domain,
                Account.url,
            )
            .join(Account, Account.id == Mention.account_id)
            .where(Mention.status_id.in_(status_ids))
            .order_by(Mention.id.asc())
        )
    ).all()
    out: dict[int, list[MentionEntry]] = {}
    for sid, aid, username, domain, url in rows:
        acct = username if domain is None else f"{username}@{domain}"
        if url:
            href = url
        elif domain:
            href = f"https://{domain}/@{username}"
        else:
            href = f"/@{username}"
        out.setdefault(sid, []).append(MentionEntry(id=str(aid), username=username, acct=acct, url=href))
    return out


def status_ids_for_batch(statuses: Iterable[object]) -> list[int]:
    """Collect ids of both the surface status and any nested reblog parents."""
    out: list[int] = []
    for status in statuses:
        sid = getattr(status, "id", None)
        if sid is not None:
            out.append(sid)
        reblog = getattr(status, "reblog", None)
        if reblog is not None:
            rid = getattr(reblog, "id", None)
            if rid is not None:
                out.append(rid)
    return out
