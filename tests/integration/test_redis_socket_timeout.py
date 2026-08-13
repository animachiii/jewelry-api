"""Regression test for the production incident on 2026-08-13: a
BACKGROUND_REMOVAL sub-job hung at GENERATING for 12+ minutes with zero log
output, wedging the entire single-worker queue (IO_QUEUE_CONCURRENCY=1).

Root cause: `redis.asyncio.Redis` has no socket timeout by default
(`socket_timeout=None`, `socket_connect_timeout=None`) -- confirmed against
the installed redis-py, not assumed. `rate_limiter.acquire()`'s
`await redis_client.incr(key)` sat on a silently stalled TCP connection to
Upstash (no RST, no FIN) with nothing to bound it. See
`app/core/redis_client.py::new_redis_client`'s docstring for the fix.

This test does not touch real Redis or the production Upstash instance --
it opens a real local "black hole" TCP listener that accepts a connection
and never reads or writes, which is exactly what an unbounded client sees
against a silently stalled remote. Two branches, same server:

- **with** `REDIS_SOCKET_TIMEOUT_SECONDS` applied (the fix): the operation
  raises within a bounded window.
- **without** it (the pre-fix construction): wrapped in a short outer
  `asyncio.wait_for` as the test's own safety net, not the thing being
  tested -- asserting *that* fires proves the inner client did not
  self-terminate on its own, i.e. it was genuinely about to hang.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import redis.asyncio as redis

from app.config import settings

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def black_hole_port() -> AsyncIterator[int]:
    """A real TCP listener that accepts a connection and never reads or
    writes -- exactly what an unbounded client sees against a silently
    stalled remote. `server.close()` only, deliberately not `await
    server.wait_closed()`: the latter can block on the per-connection
    handler task (stuck in its own `sleep(3600)`) rather than returning once
    the listening socket stops accepting -- confirmed by a standalone
    reproduction outside pytest during this test's own development, not
    assumed. The event loop's own teardown at test end reclaims the rest.
    """

    async def _accept_and_never_respond(
        _reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
    ) -> None:
        await asyncio.sleep(3600)

    server = await asyncio.start_server(_accept_and_never_respond, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()


async def test_socket_timeout_bounds_a_stalled_connection(black_hole_port: int) -> None:
    """The fix: a client built with REDIS_SOCKET_TIMEOUT_SECONDS raises
    within a bounded window against a connection that never responds."""
    client = redis.Redis(
        host="127.0.0.1",
        port=black_hole_port,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        decode_responses=True,
    )
    try:
        started = time.monotonic()
        with pytest.raises((redis.TimeoutError, redis.ConnectionError, TimeoutError)):
            await client.ping()
        elapsed = time.monotonic() - started

        # Generous slack for CI jitter -- the point is "bounded", not "exact".
        # Production is 5s; a job that used to hang for 12+ minutes failing
        # this at, say, 20s would still be a real regression worth catching.
        assert elapsed < settings.REDIS_SOCKET_TIMEOUT_SECONDS + 15
    finally:
        await client.aclose()


async def test_without_the_timeout_the_same_call_does_not_self_terminate(
    black_hole_port: int,
) -> None:
    """The bug, characterised: a client with NO socket timeout configured
    (the exact pre-fix construction) does not raise on its own against a
    stalled connection -- proven by wrapping it in a short outer
    `asyncio.wait_for` and asserting *that* is what fires, not the client
    itself. This is what actually hung in production."""
    client = redis.Redis(host="127.0.0.1", port=black_hole_port, decode_responses=True)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.ping(), timeout=3)
    finally:
        await client.aclose()
