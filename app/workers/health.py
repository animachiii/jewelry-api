"""Trivial verification task for the worker (Phase 0 Step 4).

Not in the original CLAUDE.md folder listing — added because Step 4 asked
for a verification task and it doesn't belong in generation.py or qa.py.
`ping_gpu` was removed when local matting was dropped (see
docs/decisions/0001-drop-local-matting.md) — there is no gpu queue anymore.
"""

import os

from app.workers.celery_app import celery_app


@celery_app.task(name="health.ping_io")  # type: ignore[untyped-decorator]
def ping_io() -> dict[str, object]:
    return {"queue": "io", "pid": os.getpid()}
