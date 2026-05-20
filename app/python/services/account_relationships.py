"""Batched account-relationship lookup."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.models import (
    AccountNote,
    AccountPin,
    Block,
    Follow,
    FollowRequest,
    Mute,
)


@dataclass(slots=True)
class FollowRow:
    show_reblogs: bool
    notify: bool
    languages: list[str] | None


@dataclass(slots=True)
class MuteRow:
    hide_notifications: bool
    expires_at: datetime | None


@dataclass(slots=True)
class AccountRelationships:
    following_ids: dict[int, FollowRow] = field(default_factory=dict)
    followed_by_ids: set[int] = field(default_factory=set)
    requested_ids: dict[int, FollowRow] = field(default_factory=dict)
    requested_by_ids: set[int] = field(default_factory=set)
    blocking_ids: set[int] = field(default_factory=set)
    blocked_by_ids: set[int] = field(default_factory=set)
    muting_ids: dict[int, MuteRow] = field(default_factory=dict)
    notes: dict[int, str] = field(default_factory=dict)
    endorsed_ids: set[int] = field(default_factory=set)

    def for_account(self, target_id: int) -> dict[str, object]:
        follow_settings = self.following_ids.get(target_id)
        request_settings = self.requested_ids.get(target_id)
        mute = self.muting_ids.get(target_id)
        return {
            "following": follow_settings is not None,
            "showing_reblogs": (follow_settings.show_reblogs if follow_settings else False)
            or (request_settings.show_reblogs if request_settings else False),
            "notifying": (follow_settings.notify if follow_settings else False)
            or (request_settings.notify if request_settings else False),
            "languages": (follow_settings.languages if follow_settings else None)
            or (request_settings.languages if request_settings else None),
            "followed_by": target_id in self.followed_by_ids,
            "requested": target_id in self.requested_ids,
            "requested_by": target_id in self.requested_by_ids,
            "blocking": target_id in self.blocking_ids,
            "blocked_by": target_id in self.blocked_by_ids,
            "muting": mute is not None,
            "muting_notifications": mute.hide_notifications if mute else False,
            "muting_expires_at": mute.expires_at if mute else None,
            "note": self.notes.get(target_id, ""),
            "endorsed": target_id in self.endorsed_ids,
        }


_EMPTY = AccountRelationships()


async def load_account_relationships(
    session: AsyncSession,
    viewer_account_id: int | None,
    account_ids: Iterable[int],
) -> AccountRelationships:
    ids = list(account_ids)
    if not ids or viewer_account_id is None:
        return _EMPTY

    outgoing_follows = (
        await session.execute(
            select(
                Follow.target_account_id,
                Follow.show_reblogs,
                Follow.notify,
                Follow.languages,
            ).where(
                Follow.account_id == viewer_account_id,
                Follow.target_account_id.in_(ids),
            )
        )
    ).all()
    incoming_follows = (
        await session.execute(
            select(Follow.account_id).where(
                Follow.target_account_id == viewer_account_id,
                Follow.account_id.in_(ids),
            )
        )
    ).scalars().all()
    outgoing_requests = (
        await session.execute(
            select(
                FollowRequest.target_account_id,
                FollowRequest.show_reblogs,
                FollowRequest.notify,
                FollowRequest.languages,
            ).where(
                FollowRequest.account_id == viewer_account_id,
                FollowRequest.target_account_id.in_(ids),
            )
        )
    ).all()
    incoming_requests = (
        await session.execute(
            select(FollowRequest.account_id).where(
                FollowRequest.target_account_id == viewer_account_id,
                FollowRequest.account_id.in_(ids),
            )
        )
    ).scalars().all()
    outgoing_blocks = (
        await session.execute(
            select(Block.target_account_id).where(
                Block.account_id == viewer_account_id,
                Block.target_account_id.in_(ids),
            )
        )
    ).scalars().all()
    incoming_blocks = (
        await session.execute(
            select(Block.account_id).where(
                Block.target_account_id == viewer_account_id,
                Block.account_id.in_(ids),
            )
        )
    ).scalars().all()
    outgoing_mutes = (
        await session.execute(
            select(
                Mute.target_account_id,
                Mute.hide_notifications,
                Mute.expires_at,
            ).where(
                Mute.account_id == viewer_account_id,
                Mute.target_account_id.in_(ids),
            )
        )
    ).all()
    notes = (
        await session.execute(
            select(AccountNote.target_account_id, AccountNote.comment).where(
                AccountNote.account_id == viewer_account_id,
                AccountNote.target_account_id.in_(ids),
            )
        )
    ).all()
    endorsed_targets = (
        await session.execute(
            select(AccountPin.target_account_id).where(
                AccountPin.account_id == viewer_account_id,
                AccountPin.target_account_id.in_(ids),
            )
        )
    ).scalars().all()

    return AccountRelationships(
        following_ids={
            tid: FollowRow(show_reblogs=sr, notify=n, languages=langs)
            for tid, sr, n, langs in outgoing_follows
        },
        followed_by_ids=set(incoming_follows),
        requested_ids={
            tid: FollowRow(show_reblogs=sr, notify=n, languages=langs)
            for tid, sr, n, langs in outgoing_requests
        },
        requested_by_ids=set(incoming_requests),
        blocking_ids=set(outgoing_blocks),
        blocked_by_ids=set(incoming_blocks),
        muting_ids={
            tid: MuteRow(hide_notifications=hide, expires_at=expires)
            for tid, hide, expires in outgoing_mutes
        },
        notes={tid: comment for tid, comment in notes},
        endorsed_ids=set(endorsed_targets),
    )
