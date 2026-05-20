"""arq worker configuration.

`bin/dev` boots the worker via:

    arq app.python.workers.arq_settings.WorkerSettings

arq reads `WorkerSettings.functions` for the dispatch table and
`WorkerSettings.redis_settings` for the broker. We point Redis at the
same instance Sidekiq uses so jobs enqueued by either side land on the
same queue during the Rails→Python cutover.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arq.connections import RedisSettings

from app.python.settings import get_settings
from app.python.workers.delivery import deliver_activity
from app.python.workers.media import prepare_media_attachment

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


def _redis_settings() -> RedisSettings:
    s = get_settings()
    return RedisSettings(
        host=s.redis_host,
        port=s.redis_port,
        password=s.redis_password,
        # arq prepends `s.queue_name`; we don't isolate per-worker queues
        # yet, so namespace isolation is just Redis DB selection at most.
    )


class WorkerSettings:
    """Container arq's CLI loads via dotted-path lookup. Class attrs only
    — arq inspects the class itself, not an instance."""

    redis_settings = _redis_settings()
    functions: list[Callable[..., Any]] = [
        prepare_media_attachment,
        deliver_activity,
    ]
    # arq's default polling interval is fine for the volumes we expect
    # in dev. Production tuning happens via env later.
    max_jobs = 10
    keep_result = 0  # discard results — workers fire-and-forget for now.
