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

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.pagination import (
    PageParams,
    apply_pagination,
    build_link_header,
    maybe_reverse,
    page_params,
)
from app.python.common.snowflake import now_id
from app.python.deps import CurrentAccount, DBSession
from app.python.models import (
    ACTIVITY_TYPE_FOR,
    Account,
    Favourite,
    Mention,
    Notification,
    NotificationType,
    Status,
    User,
    WebSetting,
)
from app.python.schemas.account import serialize_account
from app.python.schemas.notification import Notification_, serialize_notification
from app.python.schemas.status import serialize_status
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


@router.get("/requests", response_model=list[dict])
async def notification_requests(account: CurrentAccount) -> list[dict]:
    """Notification requests from accounts not yet approved."""
    return []


# Bulk operations — must be defined BEFORE the /{request_id} param route
@router.post("/requests/accept", status_code=200)
async def bulk_accept_notification_requests(account: CurrentAccount) -> dict:
    return {}


@router.post("/requests/dismiss", status_code=200)
async def bulk_dismiss_notification_requests(account: CurrentAccount) -> dict:
    return {}


@router.get("/requests/{request_id}", response_model=dict)
async def show_notification_request(
    request_id: int,
    account: CurrentAccount,
) -> dict:
    raise HTTPException(status_code=404, detail="Record not found")


@router.post("/requests/{request_id}/accept", status_code=200)
async def accept_notification_request(request_id: int, account: CurrentAccount) -> dict:
    return {}


@router.post("/requests/{request_id}/dismiss", status_code=200)
async def dismiss_notification_request(request_id: int, account: CurrentAccount) -> dict:
    return {}


_POLICY_DEFAULTS: dict = {
    "filter_not_following": False,
    "filter_not_followers": False,
    "filter_new_accounts": False,
    "filter_private_mentions": True,
    "summary": {
        "pending_requests_count": 0,
        "pending_notifications_count": 0,
    },
}

_POLICY_BOOL_KEYS = frozenset(
    {"filter_not_following", "filter_not_followers", "filter_new_accounts", "filter_private_mentions"}
)


class NotificationPolicyUpdate(BaseModel):
    filter_not_following: bool | None = None
    filter_not_followers: bool | None = None
    filter_new_accounts: bool | None = None
    filter_private_mentions: bool | None = None


async def _get_or_create_web_setting(
    session: AsyncSession, account_id: int
) -> WebSetting:
    """Fetch or create the WebSetting row for the given account_id."""
    row = (
        await session.execute(
            select(WebSetting)
            .join(User, User.id == WebSetting.user_id)
            .where(User.account_id == account_id)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row

    user = (
        await session.execute(select(User).where(User.account_id == account_id))
    ).scalar_one_or_none()
    if user is None:
        # Remote account — no local user; return a transient stub (never persisted).
        now = datetime.now(UTC).replace(tzinfo=None)
        return WebSetting(id=0, user_id=0, data={}, created_at=now, updated_at=now)

    now = datetime.now(UTC).replace(tzinfo=None)
    row = WebSetting(id=now_id(), user_id=user.id, data={}, created_at=now, updated_at=now)
    session.add(row)
    await session.flush()
    return row


async def _get_notification_policy(session: AsyncSession, account_id: int) -> dict:
    """Return the persisted notification policy, merged with defaults."""
    ws = await _get_or_create_web_setting(session, account_id)
    data = ws.data or {}
    stored = data.get("notification_policy", {})
    return {**_POLICY_DEFAULTS, **{k: v for k, v in stored.items() if k in _POLICY_BOOL_KEYS}}


async def _save_notification_policy(
    session: AsyncSession, account_id: int, policy: dict
) -> None:
    """Persist the notification policy into the web_settings row."""
    ws = await _get_or_create_web_setting(session, account_id)
    if ws.user_id == 0:
        return
    data = dict(ws.data or {})
    data["notification_policy"] = {k: policy[k] for k in _POLICY_BOOL_KEYS if k in policy}
    ws.data = data
    ws.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()


@router.get("/policy", response_model=dict)
async def notification_policy_v1(
    account: CurrentAccount,
    session: DBSession,
) -> dict:
    return await _get_notification_policy(session, account.id)


@router.put("/policy", response_model=dict)
async def update_notification_policy_v1(
    account: CurrentAccount,
    session: DBSession,
    body: NotificationPolicyUpdate,
) -> dict:
    current = await _get_notification_policy(session, account.id)
    updates = body.model_dump(exclude_none=True)
    merged = {**current, **{k: v for k, v in updates.items() if k in _POLICY_BOOL_KEYS}}
    await _save_notification_policy(session, account.id, merged)
    return merged


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


# ---------- /api/v2/notifications ----------

v2_router = APIRouter(prefix="/api/v2/notifications", tags=["notifications"])

# Types that group by (type + status) rather than being unique per event.
_GROUPABLE_BY_STATUS = {"favourite", "reblog"}
# Types that each get their own ungrouped key (no meaningful grouping).
_ALWAYS_UNGROUPED = {"follow", "follow_request", "mention", "poll", "update", "admin.sign_up"}
# Maximum number of sample account IDs surfaced per notification group.
_MAX_SAMPLE_ACCOUNTS = 3


@v2_router.get("", response_model=dict)
async def list_notifications_v2(
    request: Request,
    response: Response,
    session: DBSession,
    viewer: CurrentAccount,
    params: Annotated[PageParams, Depends(page_params)],
    grouped_types: list[str] = Query(default_factory=list, alias="grouped_types[]"),
    exclude_types: list[str] = Query(default_factory=list, alias="exclude_types[]"),
) -> dict:
    """Grouped notifications (v2).

    Groups favourite/reblog notifications by (type, status_id); all other
    types each form their own single-notification group.
    """
    stmt = select(Notification).where(Notification.account_id == viewer.id)
    if grouped_types:
        stmt = stmt.where(Notification.type.in_(grouped_types))
    if exclude_types:
        stmt = stmt.where(Notification.type.not_in(exclude_types))

    stmt = apply_pagination(stmt, Notification.id, params)
    rows = (await session.execute(stmt)).unique().scalars().all()
    ordered: list[Notification] = maybe_reverse(rows, params)

    # Resolve statuses for the full page (reuse existing batch loader).
    statuses_by_notification = await _resolve_statuses(session, ordered)

    # ---- Build groups ------------------------------------------------
    # key → list[Notification] in descending order (plain dict preserves insertion order)
    group_map: dict[str, list[Notification]] = {}
    for n in ordered:
        ntype = n.type or "unknown"
        resolved_status = statuses_by_notification.get(n.id)
        if ntype in _GROUPABLE_BY_STATUS and resolved_status is not None:
            # Group by (type, status_id)
            key = f"{ntype}-{resolved_status.id}"
        else:
            # Each notification is its own group
            key = f"ungrouped-{n.id}"
        group_map.setdefault(key, []).append(n)

    # ---- Collect sideload IDs ----------------------------------------
    all_account_ids: list[int] = []
    all_status_ids: list[int] = []
    seen_account_ids: set[int] = set()
    seen_status_ids: set[int] = set()

    notification_groups: list[dict] = []

    for group_key, group_notifications in group_map.items():
        # group_notifications is ordered descending (most-recent first)
        most_recent = group_notifications[0]
        oldest = group_notifications[-1]

        ntype = most_recent.type or "unknown"

        # Collect sample account ids (up to 3, most recent actors first)
        sample_account_ids: list[str] = []
        seen_in_group: set[int] = set()
        for n in group_notifications:
            if n.from_account_id not in seen_in_group:
                sample_account_ids.append(str(n.from_account_id))
                seen_in_group.add(n.from_account_id)
                if n.from_account_id not in seen_account_ids:
                    all_account_ids.append(n.from_account_id)
                    seen_account_ids.add(n.from_account_id)
            if len(sample_account_ids) >= _MAX_SAMPLE_ACCOUNTS:
                break

        # Status sideload
        status_id_str: str | None = None
        resolved_status = statuses_by_notification.get(most_recent.id)
        if resolved_status is not None:
            status_id_str = str(resolved_status.id)
            if resolved_status.id not in seen_status_ids:
                all_status_ids.append(resolved_status.id)
                seen_status_ids.add(resolved_status.id)

        notification_groups.append(
            {
                "group_key": group_key,
                "notifications_count": len(group_notifications),
                "type": ntype,
                "most_recent_notification_id": str(most_recent.id),
                "page_min_id": str(oldest.id),
                "page_max_id": str(most_recent.id),
                "latest_page_notification_at": most_recent.created_at.isoformat() + "Z",
                "sample_account_ids": sample_account_ids,
                **({"status_id": status_id_str} if status_id_str is not None else {}),
            }
        )

    # ---- Sideload accounts -------------------------------------------
    account_rows = (
        (
            await session.execute(
                select(Account).where(Account.id.in_(all_account_ids))
            )
        )
        .unique()
        .scalars()
        .all()
        if all_account_ids
        else []
    )
    accounts_out = [serialize_account(a).model_dump() for a in account_rows]

    # ---- Sideload statuses ------------------------------------------
    relationships = await load_relationships(session, viewer.id, all_status_ids)
    status_rows = (
        (
            await session.execute(
                select(Status).where(Status.id.in_(all_status_ids))
            )
        )
        .unique()
        .scalars()
        .all()
        if all_status_ids
        else []
    )
    statuses_out = [
        serialize_status(s, relationships=relationships).model_dump()
        for s in status_rows
    ]

    return {
        "accounts": accounts_out,
        "statuses": statuses_out,
        "notification_groups": notification_groups,
    }


@v2_router.get("/policy", response_model=dict)
async def notification_policy(
    account: CurrentAccount,
    session: DBSession,
) -> dict:
    return await _get_notification_policy(session, account.id)


@v2_router.put("/policy", response_model=dict)
async def update_notification_policy(
    account: CurrentAccount,
    session: DBSession,
    body: NotificationPolicyUpdate,
) -> dict:
    current = await _get_notification_policy(session, account.id)
    updates = body.model_dump(exclude_none=True)
    merged = {**current, **{k: v for k, v in updates.items() if k in _POLICY_BOOL_KEYS}}
    await _save_notification_policy(session, account.id, merged)
    return merged


