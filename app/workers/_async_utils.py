"""Shared helper for Celery task wrappers that need to run async service code.

In production, a Celery task body runs in its own worker process with no
event loop yet — plain `asyncio.run()` is correct. Under `task_always_eager`
(every test in this repo), `.delay()` executes the task body inline, and if
the caller is itself an async route handler (e.g. `POST /generate`
dispatching `orchestration.fan_out_job` — see phases/phase-7-orchestration.md),
that inline call happens *inside an already-running event loop*, where
`asyncio.run()` raises.

That case is handled by routing the coroutine onto a single persistent
background loop (a dedicated thread, started lazily and kept alive for the
process's lifetime) rather than a fresh loop per call. A fresh loop per call
was tried first and broke: `app/core/redis_client.py`'s connection is a
process-wide singleton, and asyncio/asyncpg/redis connections aren't
shareable across event loops — a fan-out that dispatches N
`generation.transform_photo` calls, each getting its own throwaway loop,
corrupts that singleton after the first call. Routing every cross-loop call
through the same background loop keeps loop-affine singletons consistent.
"""

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


def _background_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, daemon=True)
            thread.start()
            _loop = loop
            _thread = thread
        return _loop


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    future = asyncio.run_coroutine_threadsafe(coro, _background_loop())
    return future.result()
