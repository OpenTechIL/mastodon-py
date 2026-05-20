"""AP outbound JSON serializers.

Mirrors of the inbound parsers in `activity.py`: Status row → AP Note
dict, then wrapped in a Create activity. Used by:

  - The outbox collection endpoint (lists Create activities).
  - Future `FanOutOnWriteService` (signs + delivers Create activities
    to followers when a local user posts).

`to`/`cc` derivation goes the opposite direction from `_derive_visibility`:

  PUBLIC   → to=[as:Public],     cc=[<followers>]
  UNLISTED → to=[<followers>],   cc=[as:Public]
  PRIVATE  → to=[<followers>],   cc=[]
  DIRECT   → to=[explicit recipients], cc=[]   (mentions port later)

For now we only emit PUBLIC + UNLISTED — the outbox listing filters
private/direct out and PostStatus deliveries don't exist yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.python.lib.asset_urls import account_uri
from app.python.models import Visibility

if TYPE_CHECKING:
    from app.python.models import Account, Status


_AS2_CONTEXT = "https://www.w3.org/ns/activitystreams"
_AS2_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def _audience(status: Status, author_uri: str) -> tuple[list[str], list[str]]:
    """Return `(to, cc)` for `status` per Mastodon's AP convention."""
    followers = f"{author_uri}/followers"
    vis = Visibility(status.visibility)
    if vis is Visibility.PUBLIC:
        return [_AS2_PUBLIC], [followers]
    if vis is Visibility.UNLISTED:
        return [followers], [_AS2_PUBLIC]
    if vis is Visibility.PRIVATE:
        return [followers], []
    # DIRECT/LIMITED: explicit recipients populate to=, cc=[]. Mentions
    # serialization (to fill `to`) lands with the inbound mentions
    # port; for now emit an empty audience — the outbox filters these
    # visibilities out anyway.
    return [], []


def serialize_note(status: Status, author: Account) -> dict:
    """Status → AP Note dict.

    Minimum viable port — no mentions, no tag array, no attachments.
    Those serialize alongside their inbound parsers when each lands.
    """
    author_uri = account_uri(author)
    to, cc = _audience(status, author_uri)
    note: dict = {
        "id": status.uri or f"{author_uri}/statuses/{status.id}",
        "type": "Note",
        "attributedTo": author_uri,
        "content": status.text,
        "published": status.created_at.isoformat(timespec="seconds") + "Z",
        "to": to,
        "cc": cc,
        "sensitive": status.sensitive,
        "summary": status.spoiler_text or None,
    }
    if status.language:
        note["contentMap"] = {status.language: status.text}
    if status.url:
        note["url"] = status.url
    if status.in_reply_to_id:
        # We have the parent's id but not necessarily its uri here; the
        # caller fetches it when needed. The outbox listing skips
        # threading metadata for now — we'll fill `inReplyTo` once the
        # batched serializer ports.
        pass
    return note


def serialize_create_activity(status: Status, author: Account) -> dict:
    """Wrap a Note in a Create activity."""
    note = serialize_note(status, author)
    author_uri = account_uri(author)
    return {
        "@context": _AS2_CONTEXT,
        "id": f"{note['id']}/activity",
        "type": "Create",
        "actor": author_uri,
        "published": note["published"],
        "to": note["to"],
        "cc": note["cc"],
        "object": note,
    }
