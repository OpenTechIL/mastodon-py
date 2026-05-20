"""REST shape for AccountRelationship."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.python.services.account_relationships import AccountRelationships


class Relationship(BaseModel):
    id: str
    following: bool
    showing_reblogs: bool
    notifying: bool
    languages: list[str] | None
    followed_by: bool
    blocking: bool = False
    blocked_by: bool = False
    muting: bool = False
    muting_notifications: bool = False
    muting_expires_at: datetime | None = None
    requested: bool
    requested_by: bool
    domain_blocking: bool = False
    endorsed: bool = False
    note: str = ""


def serialize_relationship(
    account_id: int,
    rels: AccountRelationships,
) -> Relationship:
    state = rels.for_account(account_id)
    return Relationship(
        id=str(account_id),
        following=bool(state["following"]),
        showing_reblogs=bool(state["showing_reblogs"]),
        notifying=bool(state["notifying"]),
        languages=state["languages"],  # type: ignore[arg-type]
        followed_by=bool(state["followed_by"]),
        requested=bool(state["requested"]),
        requested_by=bool(state["requested_by"]),
        blocking=bool(state["blocking"]),
        blocked_by=bool(state["blocked_by"]),
        muting=bool(state["muting"]),
        muting_notifications=bool(state["muting_notifications"]),
        muting_expires_at=state["muting_expires_at"],  # type: ignore[arg-type]
        note=str(state["note"]),
        endorsed=bool(state["endorsed"]),
    )
