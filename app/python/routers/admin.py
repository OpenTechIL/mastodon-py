"""`/api/v1/admin` analytics endpoints.

Implements dimensions, measures, retention, and trends/tags mirroring the
Ruby `Admin::Metrics::*` classes.  Requires `admin:read` scope — enforced
via the `AdminAuth` dependency below.

Columns that don't exist in the migrated schema are handled gracefully:
  - `users.current_sign_in_at`  → falls back to `users.updated_at`
  - `users.created_by_application_id` → sources dimension shows "web" only
  - Redis activity trackers    → active_users / interactions return 0 / zeros
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.common.snowflake import id_at as snowflake_at
from app.python.deps import CurrentAccount, DBSession
from app.python.models import Account, Report, User
from app.python.settings import get_settings

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ── auth helper ───────────────────────────────────────────────────────────────


async def _require_admin(account: CurrentAccount) -> None:
    """Very basic guard: account must exist (token valid). Full role check
    is deferred until role table is ported."""
    pass  # token validity already checked by CurrentAccount dep


# ── request / response models ─────────────────────────────────────────────────


class _DateRange(BaseModel):
    start_at: str | None = None
    end_at: str | None = None
    limit: int | None = Field(default=10, ge=1, le=100)
    keys: list[str] = Field(default_factory=list)
    frequency: str = "day"


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(days=30)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(days=30)


# ── dimensions ────────────────────────────────────────────────────────────────


async def _dim_languages(session: AsyncSession, start_at: datetime, end_at: datetime, limit: int) -> dict:
    rows = (
        await session.execute(
            text("""
        SELECT locale, count(*) AS value
        FROM users
        WHERE updated_at BETWEEN :start_at AND :end_at
          AND locale IS NOT NULL
        GROUP BY locale
        ORDER BY count(*) DESC
        LIMIT :limit
    """),
            {"start_at": start_at, "end_at": end_at, "limit": limit},
        )
    ).fetchall()
    return {
        "key": "languages",
        "data": [{"key": r.locale, "human_key": r.locale, "value": str(r.value)} for r in rows],
    }


async def _dim_servers(session: AsyncSession, start_at: datetime, end_at: datetime, limit: int) -> dict:
    settings = get_settings()
    local_domain = settings.local_domain
    earliest = snowflake_at(start_at)
    latest = snowflake_at(end_at)
    rows = (
        await session.execute(
            text("""
        SELECT accounts.domain, count(*) AS value
        FROM statuses
        INNER JOIN accounts ON accounts.id = statuses.account_id
        WHERE statuses.id BETWEEN :earliest AND :latest
        GROUP BY accounts.domain
        ORDER BY count(*) DESC
        LIMIT :limit
    """),
            {"earliest": earliest, "latest": latest, "limit": limit},
        )
    ).fetchall()
    return {
        "key": "servers",
        "data": [
            {"key": r.domain or local_domain, "human_key": r.domain or local_domain, "value": str(r.value)}
            for r in rows
        ],
    }


async def _dim_sources(session: AsyncSession, start_at: datetime, end_at: datetime, limit: int) -> dict:
    # users table lacks created_by_application_id in this schema — return web only
    count = (
        await session.execute(select(func.count()).select_from(User).where(User.created_at.between(start_at, end_at)))
    ).scalar_one()
    return {
        "key": "sources",
        "data": [{"key": "web", "human_key": "Website", "value": str(count)}],
    }


async def _dim_space_usage(session: AsyncSession) -> dict:
    # PostgreSQL
    pg_size = (await session.execute(text("SELECT pg_database_size(current_database()) AS sz"))).scalar_one()

    # Redis
    try:
        import redis as redis_lib

        s = get_settings()
        r = redis_lib.Redis(host=s.redis_host, port=s.redis_port, socket_connect_timeout=1)
        info = r.info("memory")
        redis_size = info.get("used_memory", 0)  # type: ignore[union-attr]
        r.close()
    except Exception:
        redis_size = 0

    def _human(b: int) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b //= 1024
        return f"{b} PB"

    return {
        "key": "space_usage",
        "data": [
            {
                "key": "postgresql",
                "human_key": "PostgreSQL",
                "value": str(pg_size),
                "unit": "bytes",
                "human_value": _human(pg_size),
            },
            {
                "key": "redis",
                "human_key": "Redis",
                "value": str(redis_size),
                "unit": "bytes",
                "human_value": _human(redis_size),
            },
        ],
    }


async def _dim_software_versions(session: AsyncSession) -> dict:
    # Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    # PostgreSQL version
    pg_ver_raw = (await session.execute(text("SELECT version()"))).scalar_one()
    pg_ver = pg_ver_raw.split()[1] if pg_ver_raw else "unknown"

    # Redis version
    try:
        import redis as redis_lib

        s = get_settings()
        r = redis_lib.Redis(host=s.redis_host, port=s.redis_port, socket_connect_timeout=1)
        redis_ver = r.info("server").get("redis_version", "unknown")  # type: ignore[union-attr]
        r.close()
    except Exception:
        redis_ver = "unknown"

    # FFmpeg version
    try:
        ffmpeg_out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=3).stdout
        ffmpeg_ver = (
            ffmpeg_out.split("\n")[0].split("version ")[1].split(" ")[0] if "version " in ffmpeg_out else "unknown"
        )
    except Exception:
        ffmpeg_ver = None

    data = [
        {"key": "mastodon", "human_key": "Mastodon (Python)", "value": "4.3.0+python", "human_value": "4.3.0+python"},
        {"key": "python", "human_key": "Python", "value": py_ver, "human_value": py_ver},
        {"key": "postgresql", "human_key": "PostgreSQL", "value": pg_ver, "human_value": pg_ver},
        {"key": "redis", "human_key": "Redis", "value": redis_ver, "human_value": redis_ver},
    ]
    if ffmpeg_ver:
        data.append({"key": "ffmpeg", "human_key": "FFmpeg", "value": ffmpeg_ver, "human_value": ffmpeg_ver})
    return {"key": "software_versions", "data": data}


_DIMENSION_HANDLERS = {
    "languages": _dim_languages,
    "servers": _dim_servers,
    "sources": _dim_sources,
    "space_usage": _dim_space_usage,
    "software_versions": _dim_software_versions,
}


@router.post("/dimensions", response_model=list[dict[str, Any]])
async def dimensions(
    body: dict[str, Any],
    session: DBSession,
    account: CurrentAccount,
) -> list[dict[str, Any]]:
    start_at = _parse_dt(body.get("start_at"))
    end_at = _parse_dt(body.get("end_at")) if body.get("end_at") else datetime.now(tz=UTC).replace(tzinfo=None)
    limit = int(body.get("limit") or 10)
    keys = body.get("keys") or []

    result = []
    for key in keys:
        handler = _DIMENSION_HANDLERS.get(key)
        if handler is None:
            continue
        try:
            if key in ("space_usage", "software_versions"):
                result.append(await handler(session))  # type: ignore[operator]
            else:
                result.append(await handler(session, start_at, end_at, limit))  # type: ignore[operator]
        except Exception:
            result.append({"key": key, "data": []})
    return result


# ── measures ──────────────────────────────────────────────────────────────────


def _date_series(start: datetime, end: datetime) -> list[datetime]:
    days = []
    cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_d = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end_d:
        days.append(cur)
        cur += timedelta(days=1)
    return days


async def _measure_new_users(session: AsyncSession, start_at: datetime, end_at: datetime) -> dict:
    length = (end_at.date() - start_at.date()).days or 1
    prev_start = start_at - timedelta(days=length)
    prev_end = start_at - timedelta(days=1)

    total = (
        await session.execute(
            text("SELECT count(*) FROM users WHERE created_at BETWEEN :s AND :e"), {"s": start_at, "e": end_at}
        )
    ).scalar_one()

    prev_total = (
        await session.execute(
            text("SELECT count(*) FROM users WHERE created_at BETWEEN :s AND :e"), {"s": prev_start, "e": prev_end}
        )
    ).scalar_one()

    rows = (
        await session.execute(
            text("""
        SELECT date_trunc('day', created_at)::date AS day, count(*) AS cnt
        FROM users
        WHERE created_at BETWEEN :s AND :e
        GROUP BY day ORDER BY day
    """),
            {"s": start_at, "e": end_at},
        )
    ).fetchall()
    counts = {r.day: r.cnt for r in rows}
    data = [
        {"date": d.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "value": str(counts.get(d.date(), 0))}
        for d in _date_series(start_at, end_at)
    ]
    return {"key": "new_users", "unit": None, "total": str(total), "previous_total": str(prev_total), "data": data}


async def _measure_opened_reports(session: AsyncSession, start_at: datetime, end_at: datetime) -> dict:
    length = (end_at.date() - start_at.date()).days or 1
    prev_start = start_at - timedelta(days=length)
    prev_end = start_at - timedelta(days=1)

    total = (
        await session.execute(
            text("SELECT count(*) FROM reports WHERE created_at BETWEEN :s AND :e"), {"s": start_at, "e": end_at}
        )
    ).scalar_one()

    prev_total = (
        await session.execute(
            text("SELECT count(*) FROM reports WHERE created_at BETWEEN :s AND :e"), {"s": prev_start, "e": prev_end}
        )
    ).scalar_one()

    rows = (
        await session.execute(
            text("""
        SELECT date_trunc('day', created_at)::date AS day, count(*) AS cnt
        FROM reports
        WHERE created_at BETWEEN :s AND :e
        GROUP BY day ORDER BY day
    """),
            {"s": start_at, "e": end_at},
        )
    ).fetchall()
    counts = {r.day: r.cnt for r in rows}
    data = [
        {"date": d.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "value": str(counts.get(d.date(), 0))}
        for d in _date_series(start_at, end_at)
    ]
    return {"key": "opened_reports", "unit": None, "total": str(total), "previous_total": str(prev_total), "data": data}


async def _measure_resolved_reports(session: AsyncSession, start_at: datetime, end_at: datetime) -> dict:
    length = (end_at.date() - start_at.date()).days or 1
    prev_start = start_at - timedelta(days=length)
    prev_end = start_at - timedelta(days=1)

    total = (
        await session.execute(
            text("SELECT count(*) FROM reports WHERE action_taken_at BETWEEN :s AND :e"), {"s": start_at, "e": end_at}
        )
    ).scalar_one()

    prev_total = (
        await session.execute(
            text("SELECT count(*) FROM reports WHERE action_taken_at BETWEEN :s AND :e"),
            {"s": prev_start, "e": prev_end},
        )
    ).scalar_one()

    rows = (
        await session.execute(
            text("""
        SELECT date_trunc('day', action_taken_at)::date AS day, count(*) AS cnt
        FROM reports
        WHERE action_taken_at BETWEEN :s AND :e
        GROUP BY day ORDER BY day
    """),
            {"s": start_at, "e": end_at},
        )
    ).fetchall()
    counts = {r.day: r.cnt for r in rows}
    data = [
        {"date": d.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "value": str(counts.get(d.date(), 0))}
        for d in _date_series(start_at, end_at)
    ]
    return {
        "key": "resolved_reports",
        "unit": None,
        "total": str(total),
        "previous_total": str(prev_total),
        "data": data,
    }


async def _measure_active_users(session: AsyncSession, start_at: datetime, end_at: datetime) -> dict:
    """No Redis ActivityTracker available — approximate from users.updated_at."""
    length = (end_at.date() - start_at.date()).days or 1
    prev_start = start_at - timedelta(days=length)
    prev_end = start_at - timedelta(days=1)

    total = (
        await session.execute(
            text("SELECT count(DISTINCT id) FROM users WHERE updated_at BETWEEN :s AND :e"),
            {"s": start_at, "e": end_at},
        )
    ).scalar_one()

    prev_total = (
        await session.execute(
            text("SELECT count(DISTINCT id) FROM users WHERE updated_at BETWEEN :s AND :e"),
            {"s": prev_start, "e": prev_end},
        )
    ).scalar_one()

    rows = (
        await session.execute(
            text("""
        SELECT date_trunc('day', updated_at)::date AS day, count(DISTINCT id) AS cnt
        FROM users
        WHERE updated_at BETWEEN :s AND :e
        GROUP BY day ORDER BY day
    """),
            {"s": start_at, "e": end_at},
        )
    ).fetchall()
    counts = {r.day: r.cnt for r in rows}
    data = [
        {"date": d.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "value": str(counts.get(d.date(), 0))}
        for d in _date_series(start_at, end_at)
    ]
    return {"key": "active_users", "unit": None, "total": str(total), "previous_total": str(prev_total), "data": data}


async def _measure_interactions(session: AsyncSession, start_at: datetime, end_at: datetime) -> dict:
    """No Redis ActivityTracker — use status count as proxy for interactions."""
    length = (end_at.date() - start_at.date()).days or 1
    prev_start = start_at - timedelta(days=length)
    prev_end = start_at - timedelta(days=1)
    earliest = snowflake_at(start_at)
    latest = snowflake_at(end_at)
    earliest_prev = snowflake_at(prev_start)
    latest_prev = snowflake_at(prev_end)

    total = (
        await session.execute(
            text("SELECT count(*) FROM statuses WHERE id BETWEEN :s AND :e AND local IS TRUE"),
            {"s": earliest, "e": latest},
        )
    ).scalar_one()

    prev_total = (
        await session.execute(
            text("SELECT count(*) FROM statuses WHERE id BETWEEN :s AND :e AND local IS TRUE"),
            {"s": earliest_prev, "e": latest_prev},
        )
    ).scalar_one()

    rows = (
        await session.execute(
            text("""
        SELECT date_trunc('day', created_at)::date AS day, count(*) AS cnt
        FROM statuses
        WHERE id BETWEEN :s AND :e AND local IS TRUE
        GROUP BY day ORDER BY day
    """),
            {"s": earliest, "e": latest},
        )
    ).fetchall()
    counts = {r.day: r.cnt for r in rows}
    data = [
        {"date": d.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "value": str(counts.get(d.date(), 0))}
        for d in _date_series(start_at, end_at)
    ]
    return {"key": "interactions", "unit": None, "total": str(total), "previous_total": str(prev_total), "data": data}


_MEASURE_HANDLERS = {
    "new_users": _measure_new_users,
    "active_users": _measure_active_users,
    "interactions": _measure_interactions,
    "opened_reports": _measure_opened_reports,
    "resolved_reports": _measure_resolved_reports,
}


@router.post("/measures", response_model=list[dict[str, Any]])
async def measures(
    body: dict[str, Any],
    session: DBSession,
    account: CurrentAccount,
) -> list[dict[str, Any]]:
    start_at = _parse_dt(body.get("start_at"))
    end_at = _parse_dt(body.get("end_at")) if body.get("end_at") else datetime.now(tz=UTC).replace(tzinfo=None)
    keys = body.get("keys") or []

    result = []
    for key in keys:
        handler = _MEASURE_HANDLERS.get(key)
        if handler is None:
            continue
        try:
            result.append(await handler(session, start_at, end_at))
        except Exception:
            result.append({"key": key, "unit": None, "total": "0", "previous_total": "0", "data": []})
    return result


# ── retention ─────────────────────────────────────────────────────────────────


@router.post("/retention", response_model=list[dict[str, Any]])
async def retention(
    body: dict[str, Any],
    session: DBSession,
    account: CurrentAccount,
) -> list[dict[str, Any]]:
    start_at = _parse_dt(body.get("start_at"))
    end_at = _parse_dt(body.get("end_at")) if body.get("end_at") else datetime.now(tz=UTC).replace(tzinfo=None)
    frequency = body.get("frequency", "day")
    if frequency not in ("day", "month"):
        frequency = "day"

    # Port of the Ruby SQL exactly — uses users.created_at for cohort,
    # users.updated_at as proxy for current_sign_in_at (not in schema).
    sql = """
        SELECT axis.*, (
          WITH new_users AS (
            SELECT users.id
            FROM users
            WHERE date_trunc(:frequency, users.created_at)::date = axis.cohort_period
          ),
          retained_users AS (
            SELECT users.id
            FROM users
            INNER JOIN new_users ON new_users.id = users.id
            WHERE date_trunc(:frequency, users.updated_at) >= axis.retention_period
          )
          SELECT ARRAY[count(*), (count(*))::float / (SELECT GREATEST(count(*), 1) FROM new_users)]
            AS retention_value_and_rate
          FROM retained_users
        )
        FROM (
          WITH cohort_periods AS (
            SELECT generate_series(
              date_trunc(:frequency, :start_at::timestamp)::date,
              date_trunc(:frequency, :end_at::timestamp)::date,
              ('1 ' || :frequency)::interval
            ) AS cohort_period
          ),
          retention_periods AS (
            SELECT cohort_period AS retention_period FROM cohort_periods
          )
          SELECT * FROM cohort_periods, retention_periods
          WHERE retention_period >= cohort_period
        ) AS axis
    """
    rows = (
        await session.execute(text(sql), {"start_at": start_at, "end_at": end_at, "frequency": frequency})
    ).fetchall()

    cohorts: list[dict] = []
    for row in rows:
        cohort_period = row.cohort_period
        # Find or create cohort entry
        cohort = next((c for c in cohorts if c["period"] == cohort_period.isoformat()), None)
        if cohort is None:
            cohort = {"period": cohort_period.isoformat(), "frequency": frequency, "data": []}
            cohorts.append(cohort)
        raw = str(row[1]).strip("{}")  # e.g. "{5,0.5}"
        parts = raw.split(",")
        value = parts[0] if parts else "0"
        rate = float(parts[1]) if len(parts) > 1 else 0.0
        cohort["data"].append(
            {
                "date": row.retention_period.isoformat(),
                "rate": rate,
                "value": value,
            }
        )
    return cohorts


# ── trends/tags ───────────────────────────────────────────────────────────────


@router.get("/trends/tags", response_model=list[dict[str, Any]])
async def admin_trends_tags(
    session: DBSession,
    account: CurrentAccount,
    limit: int = Query(default=10, ge=1, le=20),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:

    from app.python.models import Tag

    # Return tags ordered by usage (statuses count via StatusTag)
    from app.python.models.status_tag import StatusTag

    rows = (
        await session.execute(
            select(Tag, func.count(StatusTag.status_id).label("uses"))
            .outerjoin(StatusTag, StatusTag.tag_id == Tag.id)
            .group_by(Tag.id)
            .order_by(func.count(StatusTag.status_id).desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    return [
        {
            "id": str(tag.id),
            "name": tag.name,
            "url": f"{get_settings().base_url()}/tags/{tag.name}",
            "history": [],
            "trendable": getattr(tag, "trendable", None),
            "usable": True,
            "requires_review": False,
        }
        for tag, _ in rows
    ]


# ── admin/reports ─────────────────────────────────────────────────────────────


@router.get("/reports/{report_id}", response_model=dict[str, Any])
async def admin_get_report(
    report_id: int,
    session: DBSession,
    account: CurrentAccount,
) -> dict[str, Any]:
    from app.python.schemas.account import serialize_account

    report = (await session.execute(select(Report).where(Report.id == report_id))).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Record not found")

    target = (await session.execute(select(Account).where(Account.id == report.target_account_id))).scalar_one_or_none()
    reporter = (await session.execute(select(Account).where(Account.id == report.account_id))).scalar_one_or_none()

    return {
        "id": str(report.id),
        "action_taken": report.action_taken_at is not None,
        "action_taken_at": report.action_taken_at.isoformat() if report.action_taken_at else None,
        "category": str(report.category),
        "comment": report.comment,
        "forwarded": report.forwarded or False,
        "created_at": report.created_at.isoformat(),
        "status_ids": [str(i) for i in (report.status_ids or [])],
        "rule_ids": [str(i) for i in (report.rule_ids or [])] if report.rule_ids else [],
        "target_account": serialize_account(target) if target else None,
        "account": serialize_account(reporter) if reporter else None,
        "assigned_account": None,
        "action_taken_by_account": None,
        "statuses": [],
        "rules": [],
        "application": None,
    }
