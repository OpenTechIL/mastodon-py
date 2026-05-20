"""`/api/v1/polls/{id}` + `/api/v1/polls/{id}/votes`."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.python.common.snowflake import now_id
from app.python.deps import CurrentAccount, DBSession, OptionalAuth
from app.python.models import Poll, PollVote, Status
from app.python.policies.status_policy import visible_to
from app.python.schemas.poll import Poll_, serialize_poll

router = APIRouter(prefix="/api/v1/polls", tags=["polls"])


async def _load_visible_poll(session, poll_id: int, viewer_account_id: int | None) -> Poll:
    poll = (await session.execute(select(Poll).where(Poll.id == poll_id))).scalar_one_or_none()
    if poll is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    parent = (await session.execute(select(Status).where(Status.id == poll.status_id))).scalar_one_or_none()
    if parent is None or not await visible_to(session, parent, viewer_account_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return poll


@router.get("/{poll_id}", response_model=Poll_)
async def show(
    poll_id: int,
    session: DBSession,
    auth: OptionalAuth,
) -> Poll_:
    viewer_account_id = auth.account.id if (auth and auth.account) else None
    poll = await _load_visible_poll(session, poll_id, viewer_account_id)
    own_votes = await _own_votes(session, poll.id, viewer_account_id)
    return serialize_poll(poll, viewer_account_id=viewer_account_id, own_votes=own_votes)


class VoteBody(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    choices: list[int] = Field(default_factory=list, alias="choices[]")


@router.post("/{poll_id}/votes", response_model=Poll_)
async def vote(
    poll_id: int,
    body: VoteBody,
    session: DBSession,
    voter: CurrentAccount,
) -> Poll_:
    poll = await _load_visible_poll(session, poll_id, voter.id)

    if poll.expired:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The poll has already ended")
    if not body.choices:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="choices is required")
    if not poll.multiple and len(body.choices) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="This poll only allows one choice",
        )
    if any(c < 0 or c >= len(poll.options) for c in body.choices):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid choice index")
    if len(set(body.choices)) != len(body.choices):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Duplicate choices")

    existing_choices = await _own_votes(session, poll.id, voter.id)
    if existing_choices:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="You have already voted on this poll",
        )

    now = datetime.now(tz=UTC).replace(tzinfo=None)
    # Bump in-memory tallies under the row's lock_version. Replace the
    # array wholesale so SQLAlchemy detects the change on JSON/ARRAY
    # columns (it doesn't track in-place mutations of mutable types
    # unless we hand it a brand-new list).
    new_tallies = list(poll.cached_tallies)
    for choice in body.choices:
        new_tallies[choice] += 1
        session.add(
            PollVote(
                id=now_id(),
                account_id=voter.id,
                poll_id=poll.id,
                choice=choice,
                uri=None,
                created_at=now,
                updated_at=now,
            )
        )
    poll.cached_tallies = new_tallies
    poll.votes_count = poll.votes_count + len(body.choices)
    if poll.voters_count is not None:
        poll.voters_count = poll.voters_count + 1
    poll.lock_version += 1
    poll.updated_at = now
    await session.commit()

    return serialize_poll(
        poll,
        viewer_account_id=voter.id,
        own_votes=list(body.choices),
    )


async def _own_votes(session, poll_id: int, viewer_account_id: int | None) -> list[int]:
    if viewer_account_id is None:
        return []
    rows = (
        (
            await session.execute(
                select(PollVote.choice).where(
                    PollVote.poll_id == poll_id,
                    PollVote.account_id == viewer_account_id,
                )
            )
        )
        .scalars()
        .all()
    )
    return sorted(rows)
