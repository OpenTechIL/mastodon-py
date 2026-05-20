"""REST shape for Tag."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.python.lib.asset_urls import _asset_host  # noqa: PLC2701
from app.python.models import Tag


class Tag_(BaseModel):
    id: str
    name: str
    url: str
    history: list[dict[str, Any]] = []
    following: bool = False


def serialize_tag(tag: Tag, *, following: bool = False) -> Tag_:
    return Tag_(
        id=str(tag.id),
        name=tag.display_name or tag.name,
        url=f"{_asset_host()}/tags/{tag.name}",
        history=[],  # populated when the trends pipeline ports
        following=following,
    )
