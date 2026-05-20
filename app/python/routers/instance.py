"""`/api/v1/instance` (deprecated) and `/api/v2/instance` (current).

Most fields are deployment-static and read from settings; user/status/
domain counts are computed live from the DB. Server rules, contact
account, thumbnail, and custom emoji language list emit safe defaults
until those tables/uploads port. Clients gracefully degrade.

Constants (max status length, max media attachments, pin limit, etc.)
are duplicated here from the legacy `*Validator` classes — when the
write-side phase ports those validators, the constants should be lifted
into a single `app/python/lib/limits.py` module.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import distinct, func, select

from app.python.deps import DBSession
from app.python.models import Account, Status, User
from app.python.settings import get_settings


# Hardcoded for now — see module docstring.
STATUS_MAX_CHARACTERS = 500
STATUS_MAX_MEDIA_ATTACHMENTS = 4
STATUS_URL_RESERVED_CHARS = 23
ACCOUNT_MAX_DISPLAY_NAME_LENGTH = 30
ACCOUNT_MAX_NOTE_LENGTH = 500
ACCOUNT_MAX_PINNED_STATUSES = 5
ACCOUNT_MAX_PROFILE_FIELDS = 4
DEFAULT_LANGUAGES = ["en", "ar", "ca", "cs", "de", "el", "es", "fa", "fr", "he",
                    "hi", "hu", "id", "it", "ja", "ko", "nl", "no", "pl", "pt",
                    "ro", "ru", "sv", "tr", "uk", "vi", "zh"]


router = APIRouter(tags=["instance"])


class InstanceV1(BaseModel):
    uri: str
    title: str
    short_description: str
    description: str
    email: str
    version: str
    urls: dict[str, str]
    stats: dict[str, int]
    thumbnail: str | None
    languages: list[str]
    registrations: bool
    approval_required: bool
    invites_enabled: bool
    contact_account: dict | None
    rules: list[dict]


class InstanceV2(BaseModel):
    domain: str
    title: str
    version: str
    source_url: str
    description: str
    usage: dict[str, dict]
    thumbnail: dict[str, str]
    icon: list[dict[str, str]]
    languages: list[str]
    configuration: dict[str, dict]
    registrations: dict[str, object]
    contact: dict[str, object]
    rules: list[dict]


async def _stats(session) -> dict[str, int]:
    user_count = (
        await session.execute(select(func.count()).select_from(User))
    ).scalar_one()
    status_count = (
        await session.execute(
            select(func.count())
            .select_from(Status)
            .where(Status.deleted_at.is_(None), Status.local.is_(True))
        )
    ).scalar_one()
    domain_count = (
        await session.execute(
            select(func.count(distinct(Account.domain))).where(
                Account.domain.is_not(None)
            )
        )
    ).scalar_one()
    return {
        "user_count": int(user_count or 0),
        "status_count": int(status_count or 0),
        "domain_count": int(domain_count or 0),
    }


def _streaming_url() -> str:
    settings = get_settings()
    return f"wss://{settings.effective_web_domain}"


def _source_url() -> str:
    return "https://github.com/mastodon/mastodon"


def _configuration() -> dict[str, dict]:
    return {
        "urls": {"streaming": _streaming_url()},
        "vapid": {"public_key": get_settings().vapid_public_key or ""},
        "accounts": {
            "max_display_name_length": ACCOUNT_MAX_DISPLAY_NAME_LENGTH,
            "max_note_length": ACCOUNT_MAX_NOTE_LENGTH,
            "max_featured_tags": 10,
            "max_pinned_statuses": ACCOUNT_MAX_PINNED_STATUSES,
            "max_profile_fields": ACCOUNT_MAX_PROFILE_FIELDS,
        },
        "statuses": {
            "max_characters": STATUS_MAX_CHARACTERS,
            "max_media_attachments": STATUS_MAX_MEDIA_ATTACHMENTS,
            "characters_reserved_per_url": STATUS_URL_RESERVED_CHARS,
        },
        "media_attachments": {
            "description_limit": 1500,
            "image_size_limit": 10 * 1024 * 1024,
            "image_matrix_limit": 16777216,
            "video_size_limit": 40 * 1024 * 1024,
            "video_matrix_limit": 8294400,
            "video_frame_rate_limit": 60,
            "supported_mime_types": [],  # Media phase
        },
        "polls": {
            "max_options": 4,
            "max_characters_per_option": 50,
            "min_expiration": 300,
            "max_expiration": 2629746,
        },
        "translation": {"enabled": False},
    }


@router.get("/api/v1/instance", response_model=InstanceV1)
async def instance_v1(session: DBSession) -> InstanceV1:
    settings = get_settings()
    stats = await _stats(session)
    return InstanceV1(
        uri=settings.local_domain,
        title=settings.local_domain,
        short_description="",
        description="",
        email=settings.smtp_from_address,
        version="4.3.0+python",
        urls={"streaming_api": _streaming_url()},
        stats=stats,
        thumbnail=None,
        languages=DEFAULT_LANGUAGES,
        registrations=True,
        approval_required=False,
        invites_enabled=False,
        contact_account=None,
        rules=[],
    )


@router.get("/api/v2/instance", response_model=InstanceV2)
async def instance_v2(session: DBSession) -> InstanceV2:
    settings = get_settings()
    stats = await _stats(session)
    return InstanceV2(
        domain=settings.local_domain,
        title=settings.local_domain,
        version="4.3.0+python",
        source_url=_source_url(),
        description="",
        usage={"users": {"active_month": stats["user_count"]}},
        thumbnail={"url": f"https://{settings.effective_web_domain}/preview.png"},
        icon=[
            {"src": f"https://{settings.effective_web_domain}/icon-{size}.png",
             "size": f"{size}x{size}"}
            for size in (36, 48, 72, 96, 144, 192, 256, 384, 512)
        ],
        languages=DEFAULT_LANGUAGES,
        configuration=_configuration(),
        registrations={
            "enabled": True,
            "approval_required": False,
            "message": None,
        },
        contact={"email": settings.smtp_from_address, "account": None},
        rules=[],
    )


@router.get("/api/v1/instance/peers", response_model=list[str])
async def instance_peers(session: DBSession) -> list[str]:
    """List of remote domains this server has cached accounts from.

    Distinct values of `accounts.domain` where it's not NULL — same
    derivation Mastodon uses. Anonymous-accessible. Federation peers
    typically scrape this to build their own peer graph.
    """
    rows = (
        await session.execute(
            select(distinct(Account.domain)).where(Account.domain.is_not(None))
        )
    ).scalars().all()
    return sorted(d for d in rows if d)


@router.get("/api/v1/instance/rules", response_model=list[dict[str, Any]])
async def instance_rules() -> list[dict[str, Any]]:
    """Server rules. The `rules` table isn't yet ported; returning `[]`
    keeps the React frontend's rules pane functional without crashing.
    """
    return []


# ---------- /api/v1/trends/* stubs ----------


@router.get("/api/v1/trends/tags", response_model=list[dict[str, Any]])
async def trends_tags() -> list[dict[str, Any]]:
    """Stub. The trending pipeline (Phase 5) computes weighted scores
    against the tag_trends table; until that ports we return []."""
    return []


@router.get("/api/v1/trends/statuses", response_model=list[dict[str, Any]])
async def trends_statuses() -> list[dict[str, Any]]:
    return []


@router.get("/api/v1/trends/links", response_model=list[dict[str, Any]])
async def trends_links() -> list[dict[str, Any]]:
    return []


# Mastodon API v1.4+ collapsed `/api/v1/trends` (without /tags) to mean
# `/api/v1/trends/tags`. Match the alias.
@router.get("/api/v1/trends", response_model=list[dict[str, Any]])
async def trends_legacy_alias() -> list[dict[str, Any]]:
    return []
