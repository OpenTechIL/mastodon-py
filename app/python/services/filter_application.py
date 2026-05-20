"""Apply the viewer's content filters to a status response.

The Mastodon v2 API model: a status `filtered` field is a list of
`FilterResult` objects, one per matching filter. Clients use it to
render a click-to-reveal interstitial (for `warn`) or to hide the post
entirely (for `hide`).

A filter matches when:

  1. Its `context` array contains the rendering context (e.g. "home").
  2. It hasn't expired (`expires_at` is null or in the future).
  3. At least one keyword matches the status's text or spoiler — OR —
     the status's id is in the filter's explicit status_ids set.

We load the viewer's filter set once per request and walk each status
in memory. The Rails `StatusFilter` model does the same but inside the
serializer's AR lazy-load chain; ours is explicit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.python.models import (
    CustomFilter,
    CustomFilterKeyword,
    CustomFilterStatus,
    FilterAction,
    Status,
)


@dataclass(slots=True)
class _CompiledKeyword:
    raw: str
    pattern: re.Pattern[str] | None

    def matches(self, text: str) -> bool:
        if self.pattern is not None:
            return bool(self.pattern.search(text))
        return self.raw.casefold() in text.casefold()


def _compile_keyword(kw: CustomFilterKeyword) -> _CompiledKeyword:
    raw = kw.keyword
    if not kw.whole_word:
        return _CompiledKeyword(raw=raw, pattern=None)
    # `\b` works for ASCII boundaries; close enough for the slice. CJK
    # whole-word matching needs a more sophisticated tokenizer that the
    # content-rendering phase brings in.
    return _CompiledKeyword(
        raw=raw,
        pattern=re.compile(rf"\b{re.escape(raw)}\b", re.IGNORECASE),
    )


@dataclass(slots=True)
class FilterCheck:
    filter: CustomFilter
    keywords: list[_CompiledKeyword]
    status_ids: set[int]


@dataclass(slots=True)
class FilterResult:
    filter: CustomFilter
    keyword_matches: list[str]
    status_matches: list[int]


async def load_filters_for(
    session: AsyncSession,
    viewer_account_id: int | None,
    context: str,
    *,
    now: datetime | None = None,
) -> list[FilterCheck]:
    if viewer_account_id is None:
        return []
    now = now or datetime.now(tz=timezone.utc).replace(tzinfo=None)

    filters = (
        await session.execute(
            select(CustomFilter).where(CustomFilter.account_id == viewer_account_id)
        )
    ).scalars().all()

    # Filter to the right context + not expired. The context column is a
    # string array; SQLAlchemy's ARRAY contains operator needs Postgres,
    # so we filter in Python — fine for a per-request load of a handful
    # of rows.
    relevant = [
        f
        for f in filters
        if context in (f.context or [])
        and (f.expires_at is None or f.expires_at > now)
    ]
    if not relevant:
        return []

    ids = [f.id for f in relevant]
    keyword_rows = (
        await session.execute(
            select(CustomFilterKeyword).where(
                CustomFilterKeyword.custom_filter_id.in_(ids)
            )
        )
    ).scalars().all()
    status_rows = (
        await session.execute(
            select(
                CustomFilterStatus.custom_filter_id, CustomFilterStatus.status_id
            ).where(CustomFilterStatus.custom_filter_id.in_(ids))
        )
    ).all()

    keywords_by_filter: dict[int, list[_CompiledKeyword]] = {fid: [] for fid in ids}
    for kw in keyword_rows:
        keywords_by_filter[kw.custom_filter_id].append(_compile_keyword(kw))

    status_ids_by_filter: dict[int, set[int]] = {fid: set() for fid in ids}
    for fid, sid in status_rows:
        status_ids_by_filter[fid].add(sid)

    return [
        FilterCheck(
            filter=f,
            keywords=keywords_by_filter[f.id],
            status_ids=status_ids_by_filter[f.id],
        )
        for f in relevant
    ]


def apply_filters(
    status: Status, checks: Iterable[FilterCheck]
) -> list[FilterResult]:
    out: list[FilterResult] = []
    haystack = (status.text or "") + "\n" + (status.spoiler_text or "")
    for check in checks:
        keyword_matches = [kw.raw for kw in check.keywords if kw.matches(haystack)]
        status_matched = status.id in check.status_ids
        if not keyword_matches and not status_matched:
            continue
        out.append(
            FilterResult(
                filter=check.filter,
                keyword_matches=keyword_matches,
                status_matches=[status.id] if status_matched else [],
            )
        )
    return out


def serialize_filter_result(result: FilterResult) -> dict[str, object]:
    """Inline-serializer because the schema is trivial and recursive-include
    on the full FilterSerializer would create cycles in the response."""
    return {
        "filter": {
            "id": str(result.filter.id),
            "title": result.filter.phrase,
            "context": list(result.filter.context or []),
            "expires_at": result.filter.expires_at,
            "filter_action": FilterAction(result.filter.action).name_for_api,
        },
        "keyword_matches": result.keyword_matches,
        "status_matches": [str(sid) for sid in result.status_matches],
    }
