"""Snowflake-cursor pagination matching the Mastodon API contract.

The Mastodon REST API paginates collections with these query parameters:

  - `max_id`:   return items with id strictly less than this
  - `since_id`: return items with id strictly greater than this; the result
                is ordered descending and clamped to `limit` items closest
                to `max_id`
  - `min_id`:   return items with id strictly greater than this; the result
                is ordered ASCENDING-then-reversed so the page is the items
                closest to `min_id`. This is the "load newer" cursor.

Responses include a `Link` header advertising `next` and `prev` URLs that
clients use to paginate. Tests assert exact equality with the documented
format, so this module is the only thing that builds it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import Query
from sqlalchemy import Select, asc, desc


@dataclass(frozen=True, slots=True)
class PageParams:
    max_id: int | None = None
    since_id: int | None = None
    min_id: int | None = None
    limit: int = 20


def page_params(
    max_id: int | None = Query(default=None, ge=0),
    since_id: int | None = Query(default=None, ge=0),
    min_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=20, ge=1, le=40),
) -> PageParams:
    return PageParams(max_id=max_id, since_id=since_id, min_id=min_id, limit=limit)


def apply_pagination(stmt: Select, id_column: Any, params: PageParams) -> Select:
    """Apply id-cursor filters and ordering to a snowflake-keyed query.

    Returns rows in descending id order (Mastodon's standard) except when
    `min_id` is set, in which case rows are fetched ascending and the
    caller must reverse the resulting list before serializing.
    """
    if params.max_id is not None:
        stmt = stmt.where(id_column < params.max_id)
    if params.since_id is not None:
        stmt = stmt.where(id_column > params.since_id)
    if params.min_id is not None:
        stmt = stmt.where(id_column > params.min_id)
        stmt = stmt.order_by(asc(id_column))
    else:
        stmt = stmt.order_by(desc(id_column))
    return stmt.limit(params.limit)


def maybe_reverse(rows: Sequence[Any], params: PageParams) -> list[Any]:
    """Restore descending order when the query was issued ascending (min_id)."""
    return list(reversed(rows)) if params.min_id is not None else list(rows)


def build_link_header(
    base_url: str,
    rows: Sequence[Any],
    params: PageParams,
    *,
    id_attr: str = "id",
    extra_query: dict[str, str] | None = None,
) -> str | None:
    """Build the `Link: <…>; rel="next", <…>; rel="prev"` header.

    `rows` must be ordered as returned to the client (descending id).
    Returns None when the response is empty.
    """
    if not rows:
        return None

    first_id = getattr(rows[0], id_attr)
    last_id = getattr(rows[-1], id_attr)
    extra = extra_query or {}

    def link(rel: str, **cursor: int) -> str:
        query = urlencode({**extra, **cursor, "limit": params.limit})
        return f'<{base_url}?{query}>; rel="{rel}"'

    return ", ".join([link("next", max_id=last_id), link("prev", min_id=first_id)])
