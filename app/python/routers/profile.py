"""`/api/v1/profile` — profile editing API.

Separate from `/api/v1/accounts/verify_credentials` (read-only) and
`/api/v1/accounts/update_credentials` (PATCH via multipart). This newer
endpoint accepts both JSON (regular edits) and multipart/form-data
(avatar/header image uploads from the profile editor).
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Request, status
from sqlalchemy import select

from app.python.deps import CurrentAccount, DBSession
from app.python.models import Account, FeaturedTag, Tag
from app.python.lib.asset_urls import avatar_url, header_url

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


def _serialize_profile(account: Account, featured_tags: list[Any]) -> dict[str, Any]:
    return {
        "id": str(account.id),
        "display_name": account.display_name or "",
        "note": account.note or "",
        "fields": account.fields or [],
        "avatar": avatar_url(account),
        "avatar_static": avatar_url(account, static=True),
        "avatar_description": "",
        "header": header_url(account),
        "header_static": header_url(account, static=True),
        "header_description": "",
        "locked": account.locked,
        "bot": account.bot,
        "hide_collections": account.hide_collections or False,
        "discoverable": account.discoverable or False,
        "indexable": account.indexable or False,
        "show_media": True,
        "show_media_replies": True,
        "show_featured": True,
        "attribution_domains": [],
        "featured_tags": featured_tags,
    }


@router.get("")
async def get_profile(
    session: DBSession,
    account: CurrentAccount,
) -> dict[str, Any]:
    ft_rows = (
        await session.execute(
            select(FeaturedTag, Tag)
            .join(Tag, Tag.id == FeaturedTag.tag_id)
            .where(FeaturedTag.account_id == account.id)
        )
    ).all()
    featured_tags = [
        {"id": str(ft.id), "name": tag.name, "statuses_count": 0, "last_status_at": None}
        for ft, tag in ft_rows
    ]
    return _serialize_profile(account, featured_tags)


def _coerce_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1", "yes")


async def _apply_text_fields(account: Account, data: dict[str, Any]) -> None:
    if "display_name" in data and data["display_name"] is not None:
        account.display_name = data["display_name"]
    if "note" in data and data["note"] is not None:
        account.note = data["note"]
    if "locked" in data and data["locked"] is not None:
        account.locked = bool(_coerce_bool(data["locked"]))
    if "bot" in data and data["bot"] is not None:
        account.actor_type = "Service" if _coerce_bool(data["bot"]) else "Person"
    if "discoverable" in data and data["discoverable"] is not None:
        account.discoverable = _coerce_bool(data["discoverable"])
    if "indexable" in data and data["indexable"] is not None:
        account.indexable = _coerce_bool(data["indexable"])
    if "hide_collections" in data and data["hide_collections"] is not None:
        account.hide_collections = _coerce_bool(data["hide_collections"])
    if "fields_attributes" in data and data["fields_attributes"] is not None:
        raw = data["fields_attributes"]
        if isinstance(raw, list):
            cleaned = [
                {"name": str(f.get("name", "")).strip(), "value": str(f.get("value", "")).strip()}
                for f in raw if isinstance(f, dict)
                if str(f.get("name", "")).strip() or str(f.get("value", "")).strip()
            ][:4]
            account.fields = cleaned


async def _save_image(account: Account, kind: str, file_bytes: bytes, content_type: str, filename: str) -> None:
    from app.python.storage import get_storage  # noqa: PLC0415
    ext = os.path.splitext(filename)[1] or ".jpg"
    fname = f"original{ext}"
    storage_dir = "avatars" if kind == "avatar" else "headers"
    storage = get_storage()
    # Write both original and static variants; static is the same bytes for non-GIF.
    await storage.write(f"accounts/{storage_dir}/{account.id}/original/{fname}", file_bytes)
    await storage.write(f"accounts/{storage_dir}/{account.id}/static/{fname}", file_bytes)
    setattr(account, f"{kind}_file_name", fname)
    setattr(account, f"{kind}_content_type", content_type or "image/jpeg")


@router.patch("")
async def update_profile(
    request: Request,
    session: DBSession,
    account: CurrentAccount,
) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        data: dict[str, Any] = {k: v for k, v in form.items() if not hasattr(v, "read")}
        await _apply_text_fields(account, data)

        for kind in ("avatar", "header"):
            file_field = form.get(kind)
            if file_field is not None and hasattr(file_field, "read"):
                file_bytes = await file_field.read()
                if file_bytes:
                    await _save_image(
                        account,
                        kind,
                        file_bytes,
                        getattr(file_field, "content_type", None) or "image/jpeg",
                        getattr(file_field, "filename", None) or f"{kind}.jpg",
                    )
    else:
        body = await request.body()
        data = json.loads(body) if body else {}
        await _apply_text_fields(account, data)

    await session.commit()
    await session.refresh(account)
    return _serialize_profile(account, [])


@router.delete("/avatar", status_code=status.HTTP_200_OK)
async def delete_avatar(
    session: DBSession,
    account: CurrentAccount,
) -> dict[str, Any]:
    account.avatar_file_name = None
    account.avatar_content_type = None
    account.avatar_remote_url = None
    await session.commit()
    await session.refresh(account)
    return _serialize_profile(account, [])


@router.delete("/header", status_code=status.HTTP_200_OK)
async def delete_header(
    session: DBSession,
    account: CurrentAccount,
) -> dict[str, Any]:
    account.header_file_name = None
    account.header_content_type = None
    account.header_remote_url = ""
    await session.commit()
    await session.refresh(account)
    return _serialize_profile(account, [])
