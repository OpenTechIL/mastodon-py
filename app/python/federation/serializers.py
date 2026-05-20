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

from app.python.lib.asset_urls import _asset_host, account_uri
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


_MEDIA_TYPE_MAP = {
    0: "image/jpeg",  # image
    1: "image/gif",  # gifv
    2: "video/mp4",  # video
    3: "audio/mpeg",  # audio
    4: "application/octet-stream",  # unknown
}


def serialize_note(status: Status, author: Account) -> dict:
    """Status → AP Note dict."""
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

    # inReplyTo: try the parent's uri; fall back to constructing from id
    if status.in_reply_to_id:
        parent_uri = getattr(status, "in_reply_to_uri", None)
        if not parent_uri and hasattr(status, "reblog"):
            parent_uri = None  # can't derive without a DB round-trip here
        if parent_uri:
            note["inReplyTo"] = parent_uri
        else:
            # Synthesise a local URI if the parent is local; skip if remote
            # (we'd need the parent row to know its URI).
            in_reply_to_account_id = getattr(status, "in_reply_to_account_id", None)
            if in_reply_to_account_id is None:
                host = _asset_host()
                note["inReplyTo"] = f"{host}/users/unknown/statuses/{status.in_reply_to_id}"

    # tag: mentions as Link objects + hashtags
    tags: list[dict] = []
    mentions = getattr(status, "mentions", None)
    if mentions:
        for mention in mentions:
            mentioned = getattr(mention, "account", None)
            if mentioned is not None:
                m_uri = account_uri(mentioned)
                m_url = getattr(mentioned, "url", None) or m_uri
                tags.append(
                    {
                        "type": "Mention",
                        "href": m_uri,
                        "name": f"@{mentioned.username}"
                        if not mentioned.domain
                        else f"@{mentioned.username}@{mentioned.domain}",
                    }
                )
                # ensure mentioned actor is in cc for non-public posts
                if m_uri not in cc and m_uri not in to:
                    cc.append(m_uri)
    status_tags = getattr(status, "tags", None)
    if status_tags:
        host = _asset_host()
        for tag in status_tags:
            tags.append(
                {
                    "type": "Hashtag",
                    "href": f"{host}/tags/{tag.name}",
                    "name": f"#{tag.name}",
                }
            )
    if tags:
        note["tag"] = tags

    # attachment: media attachments
    media = getattr(status, "media_attachments", None)
    if media:
        attachments = []
        for att in media:
            mt = att.file_content_type or _MEDIA_TYPE_MAP.get(att.type, "application/octet-stream")
            url = att.remote_url or (
                f"{_asset_host()}/system/media_attachments/files/{att.id}/original/{att.file_file_name}"
                if att.file_file_name
                else ""
            )
            if url:
                attachments.append(
                    {
                        "type": "Document",
                        "mediaType": mt,
                        "url": url,
                        "name": att.description or None,
                        "blurhash": getattr(att, "blurhash", None),
                    }
                )
        if attachments:
            note["attachment"] = attachments

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
