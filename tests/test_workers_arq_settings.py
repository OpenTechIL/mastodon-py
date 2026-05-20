"""Smoke tests for the arq worker scaffolding.

These don't exercise arq's runtime — they verify that `bin/dev`'s
`arq app.python.workers.arq_settings.WorkerSettings` command can resolve
the module and pull a usable settings object out of it.
"""

from __future__ import annotations


def test_worker_settings_resolves() -> None:
    from app.python.workers.arq_settings import WorkerSettings

    # arq looks at these specific class attrs at boot.
    assert WorkerSettings.functions, "no jobs registered"
    assert WorkerSettings.redis_settings is not None


def test_worker_settings_lists_prepare_media_attachment() -> None:
    from app.python.workers.arq_settings import WorkerSettings
    from app.python.workers.media import prepare_media_attachment

    assert prepare_media_attachment in WorkerSettings.functions


def test_redis_settings_reads_from_app_settings() -> None:
    from app.python.settings import get_settings
    from app.python.workers.arq_settings import WorkerSettings

    expected = get_settings()
    assert WorkerSettings.redis_settings.host == expected.redis_host
    assert WorkerSettings.redis_settings.port == expected.redis_port
