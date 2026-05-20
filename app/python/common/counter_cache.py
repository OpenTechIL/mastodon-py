"""Explicit counter-cache adjustment.

Services call `adjust_counter` when performing the action that should
move a denormalized count (`accounts.statuses_count`,
`account_stats.followers_count`, `status_stats.favourites_count`, …).
Concurrent writers from any process can race on the same row, so the
update is wrapped in a transaction-scoped Postgres advisory lock keyed
on (table, row_id) and issued as a single
`UPDATE … SET counter = counter +/- 1`.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ADVISORY_LOCK_NAMESPACE: Final[int] = 0x4D41  # "MA" — Mastodon namespace prefix


async def _acquire_lock(session: AsyncSession, table: str, row_id: int) -> None:
    # The advisory lock is a Postgres-only serializer used during the
    # legacy/Python coexistence period. On other dialects (SQLite in
    # tests, future ports to other engines) we skip it; the single-row
    # UPDATE is atomic on its own and tests run serially anyway.
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    table_key = abs(hash(table)) & 0xFFFF
    # pg_advisory_xact_lock(int8) accepts a 64-bit key; combine the
    # 16-bit table namespace and 48 low bits of row_id into one int64.
    lock_key = (table_key << 48) | (row_id & 0xFFFFFFFFFFFF)
    # Cast to signed int64 range.
    if lock_key >= (1 << 63):
        lock_key -= 1 << 64
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": lock_key},
    )


async def adjust_counter(
    session: AsyncSession,
    *,
    table: str,
    row_id: int,
    column: str,
    delta: int,
) -> None:
    """Atomically adjust `<table>.<column>` by `delta` for one row.

    Must be called inside an open transaction; the advisory lock is held
    for the remainder of the transaction.
    """
    if delta == 0:
        return
    await _acquire_lock(session, table, row_id)
    await session.execute(
        text(f"UPDATE {table} SET {column} = {column} + :delta WHERE id = :id"),  # noqa: S608
        {"delta": delta, "id": row_id},
    )
