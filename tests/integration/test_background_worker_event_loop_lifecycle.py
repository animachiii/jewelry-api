"""Phase 15 Step 5 Checkpoint 5 — app/workers/background.py builds its own
DB engine and its own owned Redis client per call, closing both, the same
pattern tests/integration/test_worker_event_loop_lifecycle.py pins for
app/workers/generation.py and app/core/redis_client.py in general.

**Plain `def`, not `async def`** — same reasoning as that file's own
docstring: only a sync test takes `_async_utils.run_async`'s real
`asyncio.run()` branch, which closes the loop on return. An `async def`
version routes onto the persistent background loop instead and would not
catch a cached-client-bound-to-a-dead-loop bug.

Calls `_run` twice with a sub-job id that doesn't exist — the point isn't
the business outcome (`SubJobNotFoundError` both times), it's that the
second call doesn't raise `RuntimeError: Event loop is closed`, which is
exactly what a shared/cached engine or Redis client across calls would do.
"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.background_service import SubJobNotFoundError
from app.workers.background import _run

pytestmark = pytest.mark.integration


def test_background_process_run_twice_does_not_reuse_a_dead_event_loop(
    db_session: AsyncSession,
) -> None:
    """`db_session` isn't used directly — depending on it triggers
    conftest.py's real schema creation and DATABASE_URL redirect to the
    testcontainer, the same setup every other real-DB test in this suite
    relies on.
    """
    missing_sub_job_id = str(uuid.uuid4())

    for _ in range(2):
        with pytest.raises(SubJobNotFoundError):
            asyncio.run(_run(missing_sub_job_id))
