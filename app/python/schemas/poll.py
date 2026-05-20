"""REST shape for Poll.

`voted` and `own_votes` are present only for authenticated viewers
(the legacy serializer omits them entirely for anon; we emit them
populated with safe defaults).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.python.models import Poll


class PollOption_(BaseModel):
    title: str
    votes_count: int | None


class Poll_(BaseModel):
    id: str
    expires_at: datetime | None
    expired: bool
    multiple: bool
    votes_count: int
    voters_count: int | None
    options: list[PollOption_]
    emojis: list[Any] = []
    voted: bool = False
    own_votes: list[int] = []


def serialize_poll(
    poll: Poll,
    *,
    viewer_account_id: int | None = None,
    own_votes: list[int] | None = None,
) -> Poll_:
    own_votes = own_votes or []
    show_totals = poll.expired or not poll.hide_totals
    options = [
        PollOption_(
            title=title,
            votes_count=(poll.cached_tallies[index] if show_totals and index < len(poll.cached_tallies) else None),
        )
        for index, title in enumerate(poll.options)
    ]
    return Poll_(
        id=str(poll.id),
        expires_at=poll.expires_at,
        expired=poll.expired,
        multiple=poll.multiple,
        votes_count=poll.votes_count,
        voters_count=poll.voters_count,
        options=options,
        voted=bool(viewer_account_id is not None and own_votes),
        own_votes=own_votes,
    )
