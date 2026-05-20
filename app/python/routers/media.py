"""`/api/v1/media` + `/api/v2/media` upload endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.python.deps import CurrentAccount, DBSession
from app.python.models import MediaAttachment, MediaType
from app.python.queue import Enqueuer, get_enqueuer
from app.python.services import media as media_service

router = APIRouter(tags=["media"])


class MediaAttachment_(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    type: str
    url: str | None
    preview_url: str | None
    remote_url: str | None
    description: str | None
    blurhash: str | None
    meta: dict[str, Any] | None = None


def _serialize(att: MediaAttachment) -> MediaAttachment_:
    return MediaAttachment_(
        id=str(att.id),
        type=MediaType(att.type).name_for_api,
        url=media_service.asset_url(att, "original"),
        preview_url=media_service.asset_url(att, "small"),
        remote_url=att.remote_url or None,
        description=att.description,
        blurhash=att.blurhash,
        meta=att.file_meta,
    )


@router.post(
    "/api/v1/media", response_model=MediaAttachment_, status_code=status.HTTP_200_OK
)
async def upload_v1(
    session: DBSession,
    account: CurrentAccount,
    file: UploadFile = File(...),
    description: str | None = Form(default=None),
) -> MediaAttachment_:
    """v1 legacy: synchronous. Returns 200 with READY state — no
    polling required. Clients that can't handle 202 still work."""
    try:
        att = await media_service.upload_media(
            session,
            author=account,
            file_name=file.filename or "upload.bin",
            content_type=file.content_type or "application/octet-stream",
            file_obj=file.file,
            description=description,
        )
    except media_service.MediaValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.detail
        ) from exc
    return _serialize(att)


@router.post(
    "/api/v2/media",
    response_model=MediaAttachment_,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_v2(
    session: DBSession,
    account: CurrentAccount,
    enqueuer: Enqueuer = Depends(get_enqueuer),
    file: UploadFile = File(...),
    description: str | None = Form(default=None),
) -> MediaAttachment_:
    """v2 async: writes the original, inserts a PROCESSING row, queues
    `prepare_media_attachment`, returns 202. The `url`/`preview_url`
    fields are null until the job flips the row to READY; clients poll
    `GET /api/v1/media/{id}` until those URLs appear."""
    try:
        att = await media_service.upload_media_async(
            session,
            author=account,
            file_name=file.filename or "upload.bin",
            content_type=file.content_type or "application/octet-stream",
            file_obj=file.file,
            description=description,
            enqueuer=enqueuer,
        )
    except media_service.MediaValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.detail
        ) from exc
    return _serialize(att)


@router.get("/api/v1/media/{media_id}", response_model=MediaAttachment_)
@router.get("/api/v2/media/{media_id}", response_model=MediaAttachment_)
async def show(
    media_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> MediaAttachment_:
    row = (
        await session.execute(
            select(MediaAttachment).where(MediaAttachment.id == media_id)
        )
    ).scalar_one_or_none()
    if row is None or row.account_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    return _serialize(row)


class MediaUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    description: str | None = None


@router.put("/api/v1/media/{media_id}", response_model=MediaAttachment_)
async def update(
    media_id: int,
    body: MediaUpdate,
    session: DBSession,
    account: CurrentAccount,
) -> MediaAttachment_:
    row = (
        await session.execute(
            select(MediaAttachment).where(MediaAttachment.id == media_id)
        )
    ).scalar_one_or_none()
    if row is None or row.account_id != account.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Record not found")
    try:
        await media_service.update_media(
            session,
            author=account,
            attachment=row,
            description=body.description,
        )
    except media_service.MediaValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.detail
        ) from exc
    return _serialize(row)
