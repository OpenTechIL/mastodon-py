"""Tests for snowflake-cursor pagination helpers."""

from __future__ import annotations

from dataclasses import dataclass

from app.python.common.pagination import (
    PageParams,
    build_link_header,
    maybe_reverse,
)


@dataclass
class Row:
    id: int


def test_maybe_reverse_no_op_for_max_id() -> None:
    rows = [Row(3), Row(2), Row(1)]
    assert maybe_reverse(rows, PageParams(max_id=10)) == rows


def test_maybe_reverse_flips_for_min_id() -> None:
    asc_rows = [Row(1), Row(2), Row(3)]
    assert maybe_reverse(asc_rows, PageParams(min_id=0)) == [Row(3), Row(2), Row(1)]


def test_link_header_empty_rows() -> None:
    assert build_link_header("https://x/api", [], PageParams()) is None


def test_link_header_shape() -> None:
    rows = [Row(300), Row(200), Row(100)]
    header = build_link_header("https://x/api/v1/timelines/public", rows, PageParams(limit=3))
    assert header is not None
    assert 'rel="next"' in header
    assert 'rel="prev"' in header
    assert "max_id=100" in header  # next cursor points past the last row
    assert "min_id=300" in header  # prev cursor points before the first row


def test_link_header_includes_extra_query() -> None:
    rows = [Row(2), Row(1)]
    header = build_link_header(
        "https://x/api/v1/timelines/tag/foo",
        rows,
        PageParams(limit=20),
        extra_query={"local": "true"},
    )
    assert header is not None
    assert "local=true" in header
