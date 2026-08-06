"""Trivial verification tasks proving gpu/io queue isolation (Phase 0 Step 4).

Not in the original CLAUDE.md folder listing — added because Step 4 asks for
these two tasks explicitly and neither belongs in matting.py or
generation.py. Documented here since folder-structure drift must be recorded.
"""

import os

from app.workers.celery_app import celery_app


@celery_app.task(name="health.ping_gpu")  # type: ignore[untyped-decorator]
def ping_gpu() -> dict[str, object]:
    return {"queue": "gpu", "pid": os.getpid()}


@celery_app.task(name="health.ping_io")  # type: ignore[untyped-decorator]
def ping_io() -> dict[str, object]:
    return {"queue": "io", "pid": os.getpid()}
