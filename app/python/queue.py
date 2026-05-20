"""Job queue abstraction.

`Enqueuer` is the seam routers/services call to schedule a background
job. The production implementation hands the call off to arq over
Redis; tests substitute a fake recorder so we don't need a live Redis.

We deliberately don't expose arq types here — callers shouldn't care
which queue engine is wired up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from arq.connections import ArqRedis


class Enqueuer(Protocol):
    async def enqueue(self, function_name: str, *args: object) -> None: ...


class RedisEnqueuer:
    """Real arq enqueue. Pool is lazy + cached for the process lifetime.

    The pool is event-loop-bound, but for the dev/prod server it's
    bound to uvicorn's loop and lives for the process. Tests don't use
    this class — they substitute `FakeEnqueuer` via Depends override.
    """

    def __init__(self) -> None:
        self._pool: ArqRedis | None = None

    async def enqueue(self, function_name: str, *args: object) -> None:
        if self._pool is None:
            from arq import create_pool

            from app.python.workers.arq_settings import WorkerSettings

            self._pool = await create_pool(WorkerSettings.redis_settings)
        await self._pool.enqueue_job(function_name, *args)


_default: Enqueuer | None = None


def get_enqueuer() -> Enqueuer:
    """FastAPI dependency. Singleton — the pool is process-scoped."""
    global _default
    if _default is None:
        _default = RedisEnqueuer()
    return _default
