"""REST shapes for status source + edit history."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.python.lib.html import status_content_format
from app.python.models import Account, Status, StatusEdit
from app.python.schemas.account import Account_, serialize_account


class StatusSource(BaseModel):
    """Shape of `GET /api/v1/statuses/{id}/source`."""

    id: str
    text: str
    spoiler_text: str


def serialize_source(status: Status) -> StatusSource:
    return StatusSource(
        id=str(status.id),
        text=status.text,
        spoiler_text=status.spoiler_text,
    )


class StatusEdit_(BaseModel):
    """One row in `GET /api/v1/statuses/{id}/history`."""

    content: str
    spoiler_text: str
    sensitive: bool
    created_at: datetime
    account: Account_
    media_attachments: list[Any] = []
    emojis: list[Any] = []


def serialize_edit(edit: StatusEdit, account: Account) -> StatusEdit_:
    return StatusEdit_(
        content=status_content_format(edit.text),
        spoiler_text=edit.spoiler_text,
        sensitive=bool(edit.sensitive),
        created_at=edit.created_at,
        account=serialize_account(account),
    )
