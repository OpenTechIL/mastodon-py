"""Outbound fan-out: deliver one activity to many recipient inboxes.

This is the unit Mastodon's `ActivityPub::FanOutOnWriteService` runs
after a local user posts / follows / boosts / favs / deletes. It takes
an audience (a list of recipient Accounts) and a single activity, then
POSTs the signed activity to each unique inbox.

Two pieces here:

  `collect_inbox_urls(accounts)` deduplicates the audience by preferring
  the recipient's shared inbox when advertised. Multiple followers on
  the same Mastodon-flavored server share one inbox, so a single POST
  fans out to all of them — bandwidth and per-RTT cost both go down.

  `deliver_to_inboxes(...)` runs `sign_and_deliver` per inbox under a
  semaphore so we don't open hundreds of sockets at once on
  high-fan-out posts. Errors are absorbed: callers see counts, not
  exceptions.

Skipped this slice: retry policy, delivery-record persistence,
exponential backoff. Those live in the arq job wrapper that drives
this function.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.python.federation.delivery import sign_and_deliver

if TYPE_CHECKING:
    import httpx

    from app.python.models import Account


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """Aggregate result of a fan-out pass.

    `attempts` is the number of POSTs we made — equal to the unique-
    inbox count, NOT the audience size (shared inboxes collapse).
    `successes`/`failures` sum to attempts. Failures includes both 4xx/5xx
    responses and network errors — `sign_and_deliver` returns False
    indistinguishably for both, which is what fan-out actually wants.
    """

    attempts: int
    successes: int
    failures: int


def collect_inbox_urls(accounts: Iterable[Account]) -> list[str]:
    """Return the set of inbox URLs to POST to for `accounts`.

    Prefers `shared_inbox_url` when set so multiple followers on one
    Mastodon server collapse into one POST. Skips accounts with neither
    field populated (no way to deliver to them).
    """
    seen: set[str] = set()
    out: list[str] = []
    for acct in accounts:
        target = (acct.shared_inbox_url or "").strip() or (acct.inbox_url or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        out.append(target)
    return out


async def deliver_to_inboxes(
    *,
    activity: dict[str, Any],
    sender: Account,
    inbox_urls: Iterable[str],
    http_client: httpx.AsyncClient,
    max_concurrency: int = 10,
) -> DeliveryReport:
    """Sign + POST `activity` to each inbox in `inbox_urls`, in parallel.

    `max_concurrency` caps how many sockets we open at once. The default
    matches Mastodon's per-worker concurrency — high enough that a 1k-
    follower fan-out doesn't take forever, low enough that we don't
    exhaust file descriptors or trigger rate limits.
    """
    urls = list(inbox_urls)
    if not urls:
        return DeliveryReport(attempts=0, successes=0, failures=0)

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _one(url: str) -> bool:
        async with semaphore:
            return await sign_and_deliver(
                activity=activity,
                sender=sender,
                recipient_inbox_url=url,
                http_client=http_client,
            )

    results = await asyncio.gather(*(_one(u) for u in urls))
    successes = sum(1 for r in results if r)
    return DeliveryReport(
        attempts=len(urls),
        successes=successes,
        failures=len(urls) - successes,
    )
