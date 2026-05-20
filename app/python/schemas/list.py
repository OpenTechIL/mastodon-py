"""REST shape for List."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.python.models import List, RepliesPolicy


class List_(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str
    replies_policy: str
    exclusive: bool


def serialize_list(row: List) -> List_:
    return List_(
        id=str(row.id),
        title=row.title,
        replies_policy=RepliesPolicy(row.replies_policy).name_for_api,
        exclusive=row.exclusive,
    )


class ListCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(..., min_length=1)
    replies_policy: str | None = None
    exclusive: bool = False


class ListUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    replies_policy: str | None = None
    exclusive: bool | None = None


class ListMembership(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_ids: list[int] = Field(default_factory=list)
