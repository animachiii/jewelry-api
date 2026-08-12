"""The cross-event-loop bugs behind two production incidents on 2026-08-12.

**Plain `def`, not `async def` — this is the whole point.**
`_async_utils.run_async` branches on whether a loop is already running: under
an `async def` test it routes onto the persistent background loop, which is
*not* the production path and hides these bugs entirely. A Celery prefork
worker has no running loop, so it takes `asyncio.run()`, which **closes** the
loop when each task returns. Only a sync test reproduces that. (Same
reasoning as tests/integration/test_migrations.py's own "plain def" note.)

Every test here calls twice, because both bugs are invisible on the first
call: live, the first angle of a job succeeded and every angle after it died.
"""

import asyncio

import pytest

from app.core.redis_client import get_redis_client, new_redis_client

pytestmark = pytest.mark.integration


def test_shared_redis_singleton_dies_on_the_second_asyncio_run() -> None:
    """Characterisation test: pins down *why* `get_redis_client` must never be
    used from a Celery task. `asyncio.run()` closes the loop, leaving the
    cached client's connections bound to a dead one -- the next task in that
    worker process then raises `RuntimeError('Event loop is closed')`.

    Live, that error escaped the task body rather than passing through
    `generation_service`'s failure handling, so the sub-job was orphaned at
    GENERATING instead of landing on FAILED (no retry offered, stuck forever).

    If a future redis-py makes the singleton loop-agnostic this test starts
    failing -- that is a genuine signal to re-examine `new_redis_client`, not
    noise to silence.
    """

    async def _ping_shared() -> bool:
        result: bool = await get_redis_client().ping()
        return result

    assert asyncio.run(_ping_shared()) is True

    with pytest.raises(RuntimeError, match="Event loop is closed"):
        asyncio.run(_ping_shared())


def test_new_redis_client_survives_repeated_asyncio_run_calls() -> None:
    """The fix: a fresh, owned client per task, closed by that task."""

    async def _ping_fresh() -> bool:
        client = new_redis_client()
        try:
            result: bool = await client.ping()
            return result
        finally:
            await client.aclose()

    for _ in range(3):
        assert asyncio.run(_ping_fresh()) is True
