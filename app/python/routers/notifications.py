"""`/api/v1/notifications` endpoints.

The route shape mirrors the legacy backend. `types[]` / `exclude_types[]`
filter on the `type` column. `account_id` filters by actor.

The polymorphic `activity_id` is resolved here, not in the serializer,
so a 20-row notification page makes one SELECT per concrete activity
type (currently just Status — favourite/reblog notifications carry a
status reference; follow notifications don't). When mentions / polls /
admin types port, each gains one IN-clause query of its own.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, or_, select

from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.deps import CurrentAccount, DBSession
from app.python.models import (
    ACTIVITY_TYPE_FOR,
    Favourite,
    Mention,
    Notification,
    NotificationType,
    Status,
)
from app.python.schemas.notification import Notification_, serialize_notification
from app.python.services.status_relationships import load_relationships

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("", response_model=list[Notification_])
async def list_notifications(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
    types: list[str] = Query(default_factory=list, alias="types[]"),
    exclude_types: list[str] = Query(default_factory=list, alias="exclude_types[]"),
    account_id: int | None = Query(default=None),
) -> list[Notification_]:
    stmt = select(Notification).where(Notification.account_id == viewer.id)
    if types:
        stmt = stmt.where(Notification.type.in_(types))
    if exclude_types:
        stmt = stmt.where(Notification.type.not_in(exclude_types))
    if account_id is not None:
        stmt = stmt.where(Notification.from_account_id == account_id)

    stmt = apply_pagination(stmt, Notification.id, params)
    rows = (await session.execute(stmt)).unique().scalars().all()
    ordered = maybe_reverse(rows, params)

    link = build_link_header(
        str(request.url.include_query_params().replace(query="")),
        ordered,
        params,
    )
    if link:
        response.headers["Link"] = link

    statuses_by_notification = await _resolve_statuses(session, ordered)
    relationships = await load_relationships(
        session, viewer.id, [s.id for s in statuses_by_notification.values()]
    )
    return [
        serialize_notification(
            n,
            resolved_status=statuses_by_notification.get(n.id),
            relationships=relationships,
        )
        for n in ordered
    ]


@router.get("/{notification_id}", response_model=Notification_)
async def show(
    notification_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> Notification_:
    row = (
        await session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.account_id == viewer.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    statuses = await _resolve_statuses(session, [row])
    relationships = await load_relationships(
        session, viewer.id, [s.id for s in statuses.values()]
    )
    return serialize_notification(
        row,
        resolved_status=statuses.get(row.id),
        relationships=relationships,
    )


@router.post("/clear", status_code=status.HTTP_200_OK)
async def clear(
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, object]:
    await session.execute(
        delete(Notification).where(Notification.account_id == viewer.id)
    )
    await session.commit()
    return {}


@router.post("/{notification_id}/dismiss", status_code=status.HTTP_200_OK)
async def dismiss(
    notification_id: int,
    session: DBSession,
    viewer: CurrentAccount,
) -> dict[str, object]:
    await session.execute(
        delete(Notification).where(
            Notification.id == notification_id,
            Notification.account_id == viewer.id,
        )
    )
    await session.commit()
    return {}


async def _resolve_statuses(
    session, notifications: list[Notification]
) -> dict[int, Status]:
    """Polymorphic batch resolver.

    Favourite-type notifications point at a `Favourite` row whose
    `status_id` is the relevant status. Mention-type notifications
    point at a `Mention` row, same pattern. Reblog-type notifications
    point at the boost wrapper Status directly. Follow-type
    notifications have no status to surface.
    """
    favourite_ids: list[int] = []
    mention_ids: list[int] = []
    status_ids: list[int] = []
    for n in notifications:
        if n.activity_type == ACTIVITY_TYPE_FOR[NotificationType.FAVOURITE]:
            favourite_ids.append(n.activity_id)
        elif n.activity_type == ACTIVITY_TYPE_FOR[NotificationType.MENTION]:
            mention_ids.append(n.activity_id)
        elif n.activity_type == ACTIVITY_TYPE_FOR[NotificationType.REBLOG]:
            status_ids.append(n.activity_id)

    by_notification: dict[int, Status] = {}

    if favourite_ids:
        rows = (
            await session.execute(
                select(Favourite.id, Favourite.status_id).where(
                    Favourite.id.in_(favourite_ids)
                )
            )
        ).all()
        favourite_status_ids = {fav_id: status_id for fav_id, status_id in rows}
        status_ids.extend(favourite_status_ids.values())
        favourite_id_to_status_id = favourite_status_ids
    else:
        favourite_id_to_status_id = {}

    if mention_ids:
        rows = (
            await session.execute(
                select(Mention.id, Mention.status_id).where(
                    Mention.id.in_(mention_ids)
                )
            )
        ).all()
        mention_id_to_status_id = {mid: sid for mid, sid in rows}
        status_ids.extend(mention_id_to_status_id.values())
    else:
        mention_id_to_status_id = {}

    if status_ids:
        rows = (
            await session.execute(select(Status).where(Status.id.in_(status_ids)))
        ).unique().scalars().all()
        status_by_id = {s.id: s for s in rows}
    else:
        status_by_id = {}

    for n in notifications:
        if n.activity_type == ACTIVITY_TYPE_FOR[NotificationType.FAVOURITE]:
            sid = favourite_id_to_status_id.get(n.activity_id)
            if sid is not None and sid in status_by_id:
                by_notification[n.id] = status_by_id[sid]
        elif n.activity_type == ACTIVITY_TYPE_FOR[NotificationType.MENTION]:
            sid = mention_id_to_status_id.get(n.activity_id)
            if sid is not None and sid in status_by_id:
                by_notification[n.id] = status_by_id[sid]
        elif n.activity_type == ACTIVITY_TYPE_FOR[NotificationType.REBLOG]:
            s = status_by_id.get(n.activity_id)
            if s is not None:
                by_notification[n.id] = s

    return by_notification
