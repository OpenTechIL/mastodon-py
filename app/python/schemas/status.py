"""REST shape for Status.

Subset of `REST::StatusSerializer`. Nested `reblog` is recursive (one
level). Media attachments, mentions, tags, polls, quotes, cards, and
the OAuth application that posted are stubbed to safe defaults pending
their respective ports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.python.lib.html import status_content_format
from app.python.models import MediaType, Status, Visibility
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.poll import serialize_poll
from app.python.services.filter_application import (
    FilterCheck,
    apply_filters,
    serialize_filter_result,
)
from app.python.services.media import asset_url as media_asset_url
from app.python.services.status_relationships import StatusRelationships


class Status_(BaseModel):
    """Public status shape — see `Account_` for the naming rationale."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    created_at: datetime
    edited_at: datetime | None
    in_reply_to_id: str | None
    in_reply_to_account_id: str | None
    sensitive: bool
    spoiler_text: str
    visibility: str
    language: str | None
    uri: str | None
    url: str | None
    replies_count: int
    reblogs_count: int
    favourites_count: int

    account: Account_
    content: str
    reblog: "Status_ | None" = None

    # Authenticated-only relationship flags; for anon callers these are
    # absent. We always emit them as false to keep the client happy
    # (`current_user?` semantics tighten in Phase 3 when interaction
    # data is queryable).
    favourited: bool = False
    reblogged: bool = False
    muted: bool = False
    bookmarked: bool = False
    pinned: bool = False

    media_attachments: list[Any] = Field(default_factory=list)
    mentions: list[dict[str, str]] = Field(default_factory=list)
    tags: list[Any] = Field(default_factory=list)
    emojis: list[Any] = Field(default_factory=list)
    card: Any = None
    poll: Any = None
    filtered: list[Any] = Field(default_factory=list)


def serialize_status(
    status: Status,
    *,
    relationships: StatusRelationships | None = None,
    filter_checks: list[FilterCheck] | None = None,
) -> Status_:
    stat = status.stat
    visibility_name = Visibility(status.visibility).name_for_api
    flags = (
        relationships.for_status(status.id)
        if relationships is not None
        else {
            "favourited": False,
            "bookmarked": False,
            "reblogged": False,
            "pinned": False,
        }
    )

    filtered = (
        [serialize_filter_result(r) for r in apply_filters(status, filter_checks)]
        if filter_checks
        else []
    )
    mentions_for_status = (
        [
            {"id": m.id, "username": m.username, "acct": m.acct, "url": m.url}
            for m in relationships.mentions_for(status.id)
        ]
        if relationships is not None
        else []
    )
    media_for_status = (
        [
            {
                "id": str(m.id),
                "type": MediaType(m.type).name_for_api,
                "url": media_asset_url(m, "original"),
                "preview_url": media_asset_url(m, "small"),
                "remote_url": m.remote_url or None,
                "description": m.description,
                "blurhash": m.blurhash,
                "meta": m.file_meta,
            }
            for m in relationships.media_for(status.id)
        ]
        if relationships is not None
        else []
    )
    poll_obj = (
        relationships.poll_for(status.id) if relationships is not None else None
    )
    poll_for_status = None
    if poll_obj is not None:
        own_votes = (
            relationships.own_votes_for(poll_obj.id)
            if relationships is not None
            else []
        )
        # `voted=True` whenever there are any own_votes; the serializer's
        # `viewer_account_id` arg is only used to gate that flag, so we
        # pass a sentinel non-None when own_votes is non-empty.
        poll_for_status = serialize_poll(
            poll_obj,
            viewer_account_id=1 if own_votes else None,
            own_votes=own_votes,
        ).model_dump()

    return Status_(
        id=str(status.id),
        created_at=status.created_at,
        edited_at=status.edited_at,
        in_reply_to_id=str(status.in_reply_to_id) if status.in_reply_to_id else None,
        in_reply_to_account_id=(
            str(status.in_reply_to_account_id) if status.in_reply_to_account_id else None
        ),
        sensitive=status.sensitive,
        spoiler_text=status.spoiler_text,
        visibility=visibility_name,
        language=status.language,
        uri=status.uri,
        url=status.url,
        replies_count=stat.replies_count if stat else 0,
        reblogs_count=stat.reblogs_count if stat else 0,
        favourites_count=stat.favourites_count if stat else 0,
        account=serialize_account(status.account),
        content=status_content_format(status.text),
        reblog=(
            serialize_status(
                status.reblog,
                relationships=relationships,
                filter_checks=filter_checks,
            )
            if status.reblog
            else None
        ),
        favourited=flags["favourited"],
        bookmarked=flags["bookmarked"],
        reblogged=flags["reblogged"],
        pinned=flags["pinned"],
        filtered=filtered,
        mentions=mentions_for_status,
        media_attachments=media_for_status,
        poll=poll_for_status,
    )
