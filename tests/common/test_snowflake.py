"""Tests for `app.python.common.snowflake`.

The contract:

    id_at(t) in [(int(t.timestamp()) * 1000) << 16, ((int(t.timestamp())+1) * 1000) << 16)
    to_time(id) == datetime.fromtimestamp((id >> 16) // 1000, tz=UTC)

Randomness has bounded ranges, so we sample many calls and check
invariants rather than equality. Without these holding, IDs minted here
would not sort correctly against IDs already in the database and
pagination cursors would desync.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.python.common.snowflake import id_at, now_id, to_time


@pytest.mark.parametrize(
    "ts",
    [
        datetime(2016, 1, 1, tzinfo=timezone.utc),
        datetime(2020, 6, 15, 12, 34, 56, tzinfo=timezone.utc),
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        datetime(2030, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    ],
)
def test_id_at_layout(ts: datetime) -> None:
    seconds = int(ts.timestamp())
    floor_id = (seconds * 1000) << 16
    ceil_id = (((seconds + 1) * 1000) << 16) - 1

    for _ in range(64):
        value = id_at(ts)
        assert floor_id <= value <= ceil_id, (
            f"id_at({ts}) = {value} fell outside expected band "
            f"[{floor_id}, {ceil_id}]"
        )


def test_id_at_without_random_is_deterministic() -> None:
    ts = datetime(2024, 3, 1, tzinfo=timezone.utc)
    expected = (int(ts.timestamp()) * 1000) << 16
    assert id_at(ts, with_random=False) == expected
    assert id_at(ts, with_random=False) == expected


def test_to_time_round_trip() -> None:
    ts = datetime(2024, 3, 1, 8, 0, 0, tzinfo=timezone.utc)
    value = id_at(ts, with_random=False)
    assert to_time(value) == ts


def test_to_time_truncates_to_seconds() -> None:
    ts = datetime(2024, 3, 1, 8, 0, 0, 999_999, tzinfo=timezone.utc)
    value = id_at(ts, with_random=False)
    assert to_time(value) == ts.replace(microsecond=0)


def test_naive_datetime_treated_as_utc() -> None:
    naive = datetime(2024, 3, 1, 8, 0, 0)
    aware = naive.replace(tzinfo=timezone.utc)
    assert id_at(naive, with_random=False) == id_at(aware, with_random=False)


def test_now_id_is_recent() -> None:
    before = datetime.now(tz=timezone.utc) - timedelta(seconds=2)
    value = now_id()
    after = datetime.now(tz=timezone.utc) + timedelta(seconds=2)

    decoded = to_time(value)
    assert before.replace(microsecond=0) <= decoded <= after.replace(microsecond=0)


def test_ids_are_monotonic_by_second() -> None:
    earlier = id_at(datetime(2024, 1, 1, tzinfo=timezone.utc), with_random=False)
    later = id_at(datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc), with_random=False)
    assert earlier < later


def test_low_16_bits_can_span_full_range() -> None:
    ts = datetime(2024, 3, 1, tzinfo=timezone.utc)
    tails = {id_at(ts) & 0xFFFF for _ in range(4096)}
    assert len(tails) > 1000, "tail randomness collapsed to a tiny set"
