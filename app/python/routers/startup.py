"""Small endpoints every Mastodon client hits on launch.

  - `GET /api/v1/custom_emojis` — server emoji palette, public.
  - `GET /api/v1/markers` + `POST /api/v1/markers` — per-user timeline
    read positions (auth required).
  - `GET /api/v1/preferences` — the authed user's posting/reading
    defaults (auth required).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Body, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.python.common.snowflake import now_id
from app.python.deps import CurrentUser, DBSession
from app.python.lib.asset_urls import _asset_host
from app.python.models import CustomEmoji, Marker

router = APIRouter(tags=["startup"])


# ---------- /api/v1/custom_emojis ----------


class CustomEmoji_(BaseModel):
    shortcode: str
    url: str
    static_url: str
    visible_in_picker: bool
    category: str | None = None


def _emoji_url(emoji: CustomEmoji) -> str:
    if emoji.image_remote_url:
        return emoji.image_remote_url
    if emoji.image_file_name:
        return f"{_asset_host()}/system/custom_emojis/images/{emoji.id}/original/{emoji.image_file_name}"
    return f"{_asset_host()}/custom_emojis/missing.png"


@router.get("/api/v1/custom_emojis", response_model=list[CustomEmoji_])
async def custom_emojis_index(session: DBSession) -> list[CustomEmoji_]:
    rows = (
        await session.execute(
            select(CustomEmoji)
            .where(CustomEmoji.disabled.is_(False), CustomEmoji.visible_in_picker.is_(True))
            .order_by(CustomEmoji.shortcode.asc())
        )
    ).scalars().all()
    return [
        CustomEmoji_(
            shortcode=e.shortcode,
            url=_emoji_url(e),
            static_url=_emoji_url(e),  # animated/static variants port with media phase
            visible_in_picker=e.visible_in_picker,
        )
        for e in rows
    ]


# ---------- /api/v1/markers ----------

# Mastodon's `MarkersController` permits only these two timelines.
# Unknown keys on POST are silently dropped; on GET they're absent
# from the response (the query is `WHERE timeline IN (...)`).
_ALLOWED_MARKER_TIMELINES = frozenset({"home", "notifications"})


class Marker_(BaseModel):
    last_read_id: str
    version: int
    updated_at: datetime


def _marker_to_dict(m: Marker) -> Marker_:
    return Marker_(
        last_read_id=str(m.last_read_id),
        version=m.lock_version,
        updated_at=m.updated_at,
    )


@router.get("/api/v1/markers", response_model=dict[str, Marker_])
async def markers_index(
    session: DBSession,
    user: CurrentUser,
    timeline: list[str] = Query(default_factory=list, alias="timeline[]"),
) -> dict[str, Marker_]:
    """Mastodon's contract: returns a dict keyed by timeline name. Omitting
    `timeline[]` returns an empty dict — callers always list what they
    care about explicitly."""
    requested = [t for t in timeline if t in _ALLOWED_MARKER_TIMELINES]
    if not requested:
        return {}
    rows = (
        await session.execute(
            select(Marker).where(
                Marker.user_id == user.id, Marker.timeline.in_(requested)
            )
        )
    ).scalars().all()
    return {m.timeline: _marker_to_dict(m) for m in rows}


@router.post("/api/v1/markers", response_model=dict[str, Marker_])
async def markers_upsert(
    session: DBSession,
    user: CurrentUser,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Marker_]:
    """Body shape: `{home: {last_read_id: "123"}, notifications: {last_read_id: "456"}}`.

    Either key is optional. We upsert per (user_id, timeline) and bump
    `lock_version` on each write.
    """
    out: dict[str, Marker_] = {}
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    for timeline, payload in body.items():
        if timeline not in _ALLOWED_MARKER_TIMELINES:
            continue
        if not isinstance(payload, dict):
            continue
        raw = payload.get("last_read_id")
        if raw is None:
            continue
        try:
            last_read_id = int(raw)
        except (TypeError, ValueError):
            continue

        existing = (
            await session.execute(
                select(Marker).where(
                    Marker.user_id == user.id, Marker.timeline == timeline
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            row = Marker(
                id=now_id(),
                user_id=user.id,
                timeline=timeline,
                last_read_id=last_read_id,
                lock_version=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            existing.last_read_id = last_read_id
            existing.lock_version += 1
            existing.updated_at = now
            row = existing
        out[timeline] = _marker_to_dict(row)
    await session.commit()
    # Refresh updated_at / version reflections (they were set in Python; commit
    # doesn't expire because expire_on_commit=False).
    return out


# ---------- /api/v1/preferences ----------


_DEFAULT_PREFERENCES: dict[str, Any] = {
    # The keys are colon-namespaced; Mastodon clients hardcode this layout.
    "posting:default:visibility": "public",
    "posting:default:sensitive": False,
    "posting:default:language": None,
    "reading:expand:media": "default",
    "reading:expand:spoilers": False,
    "reading:autoplay:gifs": False,
}


@router.get("/api/v1/preferences")
async def preferences_index(user: CurrentUser) -> dict[str, Any]:
    """Returns the user's posting/reading preferences.

    Until `users.settings` parsing ports (which involves either decoding
    the legacy YAML-serialized blob or migrating to JSON) we return the
    defaults verbatim. The `posting:default:language` falls back to the
    user's `locale` so clients pre-fill the composer with something
    sensible.
    """
    prefs = dict(_DEFAULT_PREFERENCES)
    if user.locale:
        prefs["posting:default:language"] = user.locale
    return prefs
