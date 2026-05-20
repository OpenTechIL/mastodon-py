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

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import distinct, func, select

from app.python.deps import DBSession
from app.python.lib.asset_urls import _asset_host, avatar_url, header_url  # noqa: PLC2701
from app.python.models import Account, AccountStat, Status, StatusStat, StatusTag, Tag, User, Visibility
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
    api_versions: dict[str, int] = {"mastodon": 1}


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


async def _contact_account(session) -> dict | None:
    """Return the first local user's account as the instance contact.

    In a real deployment this would come from Setting.site_contact_username.
    Until the settings table is ported we fall back to the earliest local
    account that has an associated user (i.e. is not a remote/system account).
    """
    row = (
        await session.execute(
            select(Account, AccountStat)
            .join(User, User.account_id == Account.id)
            .outerjoin(AccountStat, AccountStat.account_id == Account.id)
            .where(Account.domain.is_(None))
            .order_by(Account.id.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    account, stat = row
    from app.python.lib.asset_urls import _asset_host
    host = _asset_host()
    return {
        "id": str(account.id),
        "username": account.username,
        "acct": account.username,
        "display_name": account.display_name or account.username,
        "locked": account.locked,
        "bot": account.bot,
        "created_at": account.created_at.isoformat(timespec="seconds") + "Z" if account.created_at else None,
        "note": account.note or "",
        "url": f"{host}/@{account.username}",
        "uri": f"{host}/users/{account.username}",
        "avatar": avatar_url(account),
        "avatar_static": avatar_url(account, static=True),
        "header": header_url(account),
        "header_static": header_url(account, static=True),
        "followers_count": stat.followers_count if stat else 0,
        "following_count": stat.following_count if stat else 0,
        "statuses_count": stat.statuses_count if stat else 0,
        "last_status_at": stat.last_status_at.date().isoformat() if (stat and stat.last_status_at) else None,
        "emojis": [],
        "fields": [],
    }


def _streaming_url() -> str:
    settings = get_settings()
    scheme = "wss" if settings.env == "production" else "ws"
    return f"{scheme}://{settings.effective_web_domain}"


def _source_url() -> str:
    return "https://github.com/OpenTechIL/mastodon-py"


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
    contact = await _contact_account(session)
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
        contact_account=contact,
        rules=[],
    )


def _thumbnail(settings) -> dict[str, str]:
    if os.path.isfile("public/preview.png"):
        return {"url": settings.base_url("/preview.png")}
    return {"url": ""}


@router.get("/api/v2/instance", response_model=InstanceV2)
async def instance_v2(session: DBSession) -> InstanceV2:
    settings = get_settings()
    stats = await _stats(session)
    contact = await _contact_account(session)
    return InstanceV2(
        domain=settings.local_domain,
        title=settings.local_domain,
        version="4.3.0+python",
        source_url=_source_url(),
        description="",
        usage={"users": {"active_month": stats["user_count"]}},
        thumbnail=_thumbnail(settings),
        icon=[
            {"src": settings.base_url(f"/icon-{size}.png"), "size": f"{size}x{size}"}
            for size in (36, 48, 72, 96, 144, 192, 256, 384, 512)
            if os.path.isfile(f"public/icon-{size}.png")
        ],
        languages=DEFAULT_LANGUAGES,
        configuration=_configuration(),
        registrations={
            "enabled": True,
            "approval_required": False,
            "message": None,
        },
        contact={"email": settings.smtp_from_address, "account": contact},
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


@router.get("/api/v1/instance/extended_description")
async def instance_extended_description() -> dict[str, Any]:
    return {"updated_at": None, "content": ""}


_PRIVACY_POLICY_UPDATED_AT = "2022-10-07T00:00:00.000Z"
_PRIVACY_POLICY_TEMPLATE: str | None = None


def _load_privacy_policy_template() -> str:
    global _PRIVACY_POLICY_TEMPLATE  # noqa: PLW0603
    if _PRIVACY_POLICY_TEMPLATE is None:
        try:
            with open("config/templates/privacy-policy.md", encoding="utf-8") as f:
                _PRIVACY_POLICY_TEMPLATE = f.read()
        except FileNotFoundError:
            _PRIVACY_POLICY_TEMPLATE = ""
    return _PRIVACY_POLICY_TEMPLATE


@router.get("/api/v1/instance/privacy_policy")
async def instance_privacy_policy() -> dict[str, Any]:
    import markdown as md  # noqa: PLC0415

    settings = get_settings()
    template = _load_privacy_policy_template()
    text = template.replace("%{domain}", settings.local_domain)
    content = md.markdown(text, extensions=["nl2br"])
    return {"updated_at": _PRIVACY_POLICY_UPDATED_AT, "content": content}


@router.get("/api/v1/instance/translation_languages")
async def instance_translation_languages() -> dict[str, Any]:
    return {}


@router.get("/api/v1/instance/domain_blocks")
async def instance_domain_blocks() -> list[dict[str, Any]]:
    return []


@router.get("/api/v1/instance/terms_of_service")
async def instance_terms_of_service() -> dict[str, Any]:
    return {"updated_at": None, "content": ""}


@router.get("/api/v1/annual_reports/{year}/state")
async def annual_report_state(year: int) -> dict[str, Any]:
    return {"year": year, "status": "unavailable"}


@router.post("/api/v1/annual_reports/{year}/generate", status_code=200)
async def annual_report_generate(year: int) -> dict[str, Any]:
    return {"year": year, "status": "unavailable"}


@router.get("/api/v1/annual_reports/{year}")
async def annual_report(year: int) -> dict[str, Any]:
    from fastapi import HTTPException
    raise HTTPException(status_code=404, detail="Not found")


@router.get("/api/v1_alpha/async_refreshes/{refresh_id}")
async def async_refresh_status(refresh_id: str) -> dict[str, Any]:
    return {"id": refresh_id, "status": "complete"}


@router.get("/api/v1/instance/rules", response_model=list[dict[str, Any]])
async def instance_rules() -> list[dict[str, Any]]:
    """Server rules. The `rules` table isn't yet ported; returning `[]`
    keeps the React frontend's rules pane functional without crashing.
    """
    return []


# ---------- /api/v1/trends/* ----------


@router.get("/api/v1/trends/statuses", response_model=list[dict[str, Any]])
async def trends_statuses(session: DBSession) -> list[dict[str, Any]]:
    """Return the 20 most-engaged public local statuses from the last 7 days."""
    from app.python.schemas.status import serialize_status  # noqa: PLC0415

    cutoff = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    stmt = (
        select(Status)
        .outerjoin(StatusStat, StatusStat.status_id == Status.id)
        .where(
            Status.visibility == Visibility.PUBLIC.value,
            Status.deleted_at.is_(None),
            Status.reblog_of_id.is_(None),
            Status.local.is_(True),
            Status.created_at > cutoff,
        )
        .order_by(
            (
                func.coalesce(StatusStat.reblogs_count, 0)
                + func.coalesce(StatusStat.favourites_count, 0)
            ).desc(),
            Status.id.desc(),
        )
        .limit(20)
    )
    rows = (await session.execute(stmt)).unique().scalars().all()
    return [serialize_status(s).model_dump() for s in rows]


@router.get("/api/v1/trends/tags", response_model=list[dict[str, Any]])
async def trends_tags(session: DBSession) -> list[dict[str, Any]]:
    """Return the 10 most-used hashtags in the last 7 days with per-day history."""
    from sqlalchemy import Integer, cast, distinct  # noqa: PLC0415
    host = _asset_host()
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=7)

    # Find the top tags by total usage in the last 7 days.
    top_stmt = (
        select(Tag.id, Tag.name, func.count(StatusTag.status_id).label("usage_count"))
        .join(StatusTag, StatusTag.tag_id == Tag.id)
        .join(Status, Status.id == StatusTag.status_id)
        .where(
            Status.visibility == Visibility.PUBLIC.value,
            Status.deleted_at.is_(None),
            Status.local.is_(True),
            Status.created_at > cutoff,
        )
        .group_by(Tag.id)
        .order_by(func.count(StatusTag.status_id).desc())
        .limit(10)
    )
    top_rows = (await session.execute(top_stmt)).all()
    if not top_rows:
        return []

    tag_ids = [row.id for row in top_rows]

    # Compute per-day uses + accounts for each tag over the last 7 days.
    # day_offset = floor((now - created_at) / 86400) → 0 = today, 6 = 6 days ago.
    epoch = datetime(1970, 1, 1)
    day_stmt = (
        select(
            StatusTag.tag_id,
            cast(
                func.floor(
                    func.extract("epoch", now - Status.created_at) / 86400
                ),
                Integer,
            ).label("day_offset"),
            func.count(StatusTag.status_id).label("uses"),
            func.count(distinct(Status.account_id)).label("accounts"),
        )
        .join(Status, Status.id == StatusTag.status_id)
        .where(
            StatusTag.tag_id.in_(tag_ids),
            Status.visibility == Visibility.PUBLIC.value,
            Status.deleted_at.is_(None),
            Status.local.is_(True),
            Status.created_at > cutoff,
        )
        .group_by(StatusTag.tag_id, "day_offset")
    )
    day_rows = (await session.execute(day_stmt)).all()

    # Build lookup: tag_id → {day_offset: {uses, accounts}}
    day_data: dict[int, dict[int, dict]] = {}
    for row in day_rows:
        day_data.setdefault(row.tag_id, {})[int(row.day_offset)] = {
            "uses": int(row.uses),
            "accounts": int(row.accounts),
        }

    result = []
    for row in top_rows:
        # Mastodon returns history newest-first: index 0 = today, index 1 = yesterday…
        history = []
        for offset in range(7):
            day_dt = now - timedelta(days=offset)
            day_ts = int((day_dt.replace(hour=0, minute=0, second=0, microsecond=0) - epoch).total_seconds())
            counts = day_data.get(row.id, {}).get(offset, {"uses": 0, "accounts": 0})
            history.append({
                "day": str(day_ts),
                "uses": str(counts["uses"]),
                "accounts": str(counts["accounts"]),
            })
        result.append({
            "name": row.name,
            "url": f"{host}/tags/{row.name}",
            "history": history,
            "following": False,
        })
    return result


@router.get("/api/v1/trends/links", response_model=list[dict[str, Any]])
async def trends_links() -> list[dict[str, Any]]:
    """No preview cards table yet — always empty."""
    return []


# Mastodon API v1.4+ collapsed `/api/v1/trends` (without /tags) to mean
# `/api/v1/trends/tags`. Match the alias.
@router.get("/api/v1/trends", response_model=list[dict[str, Any]])
async def trends_legacy_alias(session: DBSession) -> list[dict[str, Any]]:
    return await trends_tags(session)


# ---------- /api/v1/annual_reports ----------

from app.python.deps import CurrentAccount  # noqa: E402


@router.get("/api/v1/annual_reports", response_model=list[dict[str, Any]])
async def annual_reports(account: CurrentAccount) -> list[dict[str, Any]]:
    """Annual report summary (Wrapstodon). Returns empty until table is ported."""
    return []


@router.post("/api/v1/annual_reports/{year}/read", status_code=200)
async def read_annual_report(year: int, account: CurrentAccount) -> dict[str, Any]:
    return {}


@router.get("/api/v1/annual_reports/{year}/state")
async def annual_report_state(year: int, account: CurrentAccount) -> dict[str, Any]:
    return {"year": year, "state": "not_generated"}


@router.post("/api/v1/annual_reports/{year}/generate", status_code=200)
async def generate_annual_report(year: int, account: CurrentAccount) -> dict[str, Any]:
    return {}


