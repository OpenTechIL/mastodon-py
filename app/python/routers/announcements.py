"""`/api/v1/announcements` + `/announcements/{id}/dismiss`."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.python.common.snowflake import now_id
from app.python.deps import CurrentAccount, DBSession, OptionalAuth
from app.python.lib.html import status_content_format
from app.python.models import Announcement, AnnouncementMute

router = APIRouter(prefix="/api/v1/announcements", tags=["announcements"])


class Announcement_(BaseModel):
    id: str
    content: str
    starts_at: datetime | None
    ends_at: datetime | None
    all_day: bool
    published_at: datetime | None
    updated_at: datetime
    read: bool = False
    # Sub-resources we don't yet populate; clients tolerate empty lists.
    mentions: list[Any] = []
    statuses: list[Any] = []
    tags: list[Any] = []
    emojis: list[Any] = []
    reactions: list[Any] = []


def _serialize(a: Announcement, *, read: bool) -> Announcement_:
    return Announcement_(
        id=str(a.id),
        content=status_content_format(a.text),
        starts_at=a.starts_at,
        ends_at=a.ends_at,
        all_day=a.all_day,
        published_at=a.published_at,
        updated_at=a.updated_at,
        read=read,
    )


@router.get("", response_model=list[Announcement_])
async def index(
    session: DBSession,
    auth: OptionalAuth,
) -> list[Announcement_]:
    """Lists currently-active published announcements.

    "Currently active" means `published=true` AND (no end date OR end
    date is in the future). Anonymous viewers see the same list; the
    `read` flag is always `false` for them.
    """
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    rows = (
        await session.execute(
            select(Announcement)
            .where(
                Announcement.published.is_(True),
                (Announcement.ends_at.is_(None)) | (Announcement.ends_at > now),
            )
            .order_by(Announcement.published_at.desc().nulls_last())
        )
    ).scalars().all()
    if not rows:
        return []

    viewer_account_id = auth.account.id if (auth and auth.account) else None
    read_ids: set[int] = set()
    if viewer_account_id is not None:
        muted = (
            await session.execute(
                select(AnnouncementMute.announcement_id).where(
                    AnnouncementMute.account_id == viewer_account_id,
                    AnnouncementMute.announcement_id.in_([a.id for a in rows]),
                )
            )
        ).scalars().all()
        read_ids = set(muted)
    return [_serialize(a, read=a.id in read_ids) for a in rows]


@router.post("/{announcement_id}/dismiss", status_code=status.HTTP_200_OK)
async def dismiss(
    announcement_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(Announcement).where(Announcement.id == announcement_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")

    existing = (
        await session.execute(
            select(AnnouncementMute).where(
                AnnouncementMute.account_id == viewer.id,
                AnnouncementMute.announcement_id == announcement_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        session.add(
            AnnouncementMute(
                id=now_id(),
                account_id=viewer.id,
                announcement_id=announcement_id,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    return {}
