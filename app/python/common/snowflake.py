"""Snowflake IDs for timestamp-keyed tables.

Mastodon IDs are 64-bit ints with the layout:
  - high 48 bits: unix timestamp in milliseconds
  - low 16 bits:  per-row tail derived from
                  (md5(table_name || salt || ts) hi bytes
                   + nextval(<table>_id_seq)) & 0xFFFF

The Postgres `timestamp_id` PL/pgSQL function is the DEFAULT for `id`
columns on snowflake tables, so application code rarely needs to mint
IDs itself. This module exists for two cases the SQL function cannot
serve:

  1. Records carrying a backdated `created_at` (federation imports,
     sample-data scripts) — we synthesize an ID matching that timestamp.
  2. Pagination cursors — clients hand us `min_id` / `max_id` values,
     and time-band filtering needs ID ↔ timestamp conversion without a
     database round-trip.

Bit-level compatibility with IDs already in the database is mandatory:
IDs minted here must interleave correctly with IDs minted by Postgres
so that ordering and pagination cursors remain consistent.
"""

from __future__ import annotations

import secrets
import threading
from datetime import UTC, datetime


def id_at(timestamp: datetime, *, with_random: bool = True) -> int:
    """Return a 64-bit snowflake ID positioned at `timestamp`.

    The algorithm:

        id  = int(timestamp.timestamp()) * 1000   # unix seconds -> ms
        id += randbelow(1000) if with_random      # jitter within the second
        id <<= 16                                 # shift ms into high 48 bits
        id += randbelow(2**16) if with_random     # random tail in low 16 bits

    Sub-second precision in the input is intentionally dropped — we
    encode whole-second resolution so two IDs minted in the same second
    sort by their tail rather than by sub-millisecond clock drift.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    seconds = int(timestamp.timestamp())
    value = seconds * 1000
    if with_random:
        value += secrets.randbelow(1000)
    value <<= 16
    if with_random:
        value += secrets.randbelow(1 << 16)
    return value


def to_time(snowflake_id: int) -> datetime:
    """Recover the timestamp embedded in a snowflake ID.

    Inverse of `id_at`: integer-divides the high 48 bits by 1000 to get
    whole seconds, matching what `id_at` encodes.
    """
    seconds = (snowflake_id >> 16) // 1000
    return datetime.fromtimestamp(seconds, tz=UTC)


_monotonic_lock = threading.Lock()
_last_minted_id = 0


def now_id() -> int:
    """A fresh snowflake ID for the current moment.

    Strict monotonicity within a process: the Postgres `timestamp_id`
    function used in production combines a per-table sequence's
    `nextval()` into the low 16 bits, so IDs minted in the same
    millisecond are always increasing for a given table. App-side
    `now_id()` is the test-time fallback; we replicate that monotonic
    property with a process-local counter so list-ordering tests don't
    flake on sub-second writes.
    """
    global _last_minted_id
    with _monotonic_lock:
        candidate = id_at(datetime.now(tz=UTC))
        if candidate <= _last_minted_id:
            candidate = _last_minted_id + 1
        _last_minted_id = candidate
        return candidate
