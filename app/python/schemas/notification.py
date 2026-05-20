"""REST shape for Notification.

The `account` field is the actor (who favourited/followed). The `status`
field is present only for `favourite` and `reblog` types — for reblog
it's the wrapper Status (whose nested `.reblog` points at the user's
original); for favourite it's the user's status that was favourited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.python.models import Notification
from app.python.schemas.account import Account_, serialize_account
from app.python.schemas.status import Status_, serialize_status
from app.python.services.status_relationships import StatusRelationships


class Notification_(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    type: str
    created_at: datetime
    group_key: str
    account: Account_
    status: Status_ | None = None

    # Spec also includes `filtered`, `report`, `event`, `moderation_warning`
    # — none of those fire in this slice. We omit them rather than
    # defaulting; clients are expected to ignore missing optional fields.


def serialize_notification(
    notification: Notification,
    *,
    resolved_status: Any | None = None,
    relationships: StatusRelationships | None = None,
) -> Notification_:
    """Build the wire shape.

    `resolved_status` is the Status row this notification points at,
    pre-fetched by the batch loader so we don't N+1 inside the
    serializer.
    """
    return Notification_(
        id=str(notification.id),
        type=notification.type or "unknown",
        created_at=notification.created_at,
        group_key=notification.group_key or f"ungrouped-{notification.id}",
        account=serialize_account(notification.from_account),
        status=(
            serialize_status(resolved_status, relationships=relationships)
            if (notification.status_typed and resolved_status is not None)
            else None
        ),
    )
