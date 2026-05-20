"""arq job: fan-out one activity to many remote inboxes.

The Rails counterpart is `ActivityPub::FanOutOnWriteService` enqueueing
many `ActivityPub::DeliveryWorker` jobs (one per recipient). We collapse
that into a single arq job that drives `deliver_to_inboxes` over the
recipient list, because asyncio gives us cheap per-recipient concurrency
without needing one job per recipient.

`deliver_activity(ctx, activity, sender_account_id, inbox_urls)`:

  1. Open a fresh DB session and load the sender Account by id (we
     need its private_key + uri to sign).
  2. Open a fresh httpx.AsyncClient (per-job lifecycle).
  3. Call `deliver_to_inboxes`. Errors per recipient are absorbed.

Failure modes:
  - Sender not in DB → log + return without raising.
  - Sender has no private key → caller's bug; signing would fail
    anyway. Same: log + skip.

Both arq job invocation and direct callers use the same code via
`_run` so tests don't need an arq runtime.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from app.python.db import session_factory
from app.python.federation.fanout import DeliveryReport, deliver_to_inboxes
from app.python.models import Account

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_log = logging.getLogger(__name__)


async def _run(
    session: AsyncSession,
    http_client: httpx.AsyncClient,
    *,
    activity: dict[str, Any],
    sender_account_id: int,
    inbox_urls: list[str],
) -> DeliveryReport:
    """Pure-logic core. Caller owns session + http_client lifecycle."""
    sender = (
        await session.execute(
            select(Account).where(Account.id == sender_account_id)
        )
    ).scalar_one_or_none()
    if sender is None:
        _log.warning(
            "deliver_activity: sender account %s not found", sender_account_id
        )
        return DeliveryReport(attempts=0, successes=0, failures=0)
    if not sender.private_key:
        _log.warning(
            "deliver_activity: sender account %s has no private key",
            sender_account_id,
        )
        return DeliveryReport(attempts=0, successes=0, failures=0)
    return await deliver_to_inboxes(
        activity=activity,
        sender=sender,
        inbox_urls=inbox_urls,
        http_client=http_client,
    )


async def deliver_activity(
    ctx: dict[str, Any],
    activity: dict[str, Any],
    sender_account_id: int,
    inbox_urls: list[str],
) -> dict[str, int]:
    """arq entry point. Opens its own session + http client."""
    async with session_factory()() as session:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            report = await _run(
                session,
                http_client,
                activity=activity,
                sender_account_id=sender_account_id,
                inbox_urls=inbox_urls,
            )
    return {
        "attempts": report.attempts,
        "successes": report.successes,
        "failures": report.failures,
    }
