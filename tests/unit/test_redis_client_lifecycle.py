"""app/core/redis_client.py's two contracts.

The singleton is correct for FastAPI (one process-long event loop) and fatal
for Celery tasks, whose `asyncio.run()` closes the loop after every task --
leaving the cached client's connections bound to a dead loop, so the next
task in that worker process raises `RuntimeError('Event loop is closed')`.
Third occurrence of this exact bug in this codebase; see
`new_redis_client`'s docstring.
"""

from app.config import settings
from app.core.redis_client import get_redis_client, new_redis_client


def test_get_redis_client_returns_the_same_shared_instance() -> None:
    assert get_redis_client() is get_redis_client()


def test_new_redis_client_returns_a_fresh_unshared_instance() -> None:
    first = new_redis_client()
    second = new_redis_client()

    assert first is not second
    assert first is not get_redis_client()


def test_worker_tasks_do_not_use_the_shared_singleton() -> None:
    """Guards the actual regression: a worker reaching for `get_redis_client`
    reintroduces the closed-loop crash. Import-level check because the
    failure only reproduces against a real Redis across two `asyncio.run()`
    calls (see tests/integration/test_config_sync_worker.py).
    """
    import app.workers.background as background_worker
    import app.workers.config as config_worker
    import app.workers.generation as generation_worker

    for module in (config_worker, generation_worker, background_worker):
        assert not hasattr(module, "get_redis_client"), (
            f"{module.__name__} must use new_redis_client(), not the shared singleton"
        )
        assert hasattr(module, "new_redis_client")


def test_new_redis_client_applies_the_configured_socket_timeout() -> None:
    """2026-08-13 incident: redis-py's default is no socket timeout at all
    (socket_timeout=None), so a silently stalled connection blocks forever.
    See tests/integration/test_redis_socket_timeout.py for the real-hang
    reproduction; this just pins that the kwargs actually reach the client.
    """
    client = new_redis_client()
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == settings.REDIS_SOCKET_TIMEOUT_SECONDS
    assert kwargs["socket_connect_timeout"] == settings.REDIS_SOCKET_TIMEOUT_SECONDS


def test_get_redis_client_applies_the_configured_socket_timeout() -> None:
    client = get_redis_client()
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == settings.REDIS_SOCKET_TIMEOUT_SECONDS
    assert kwargs["socket_connect_timeout"] == settings.REDIS_SOCKET_TIMEOUT_SECONDS


def test_idempotency_and_ratelimit_clients_carry_the_same_timeout() -> None:
    """Both modules used to build their own raw redis.from_url(...) client
    with no timeout at all -- delegating to new_redis_client() (2026-08-13)
    means this incident's fix covers the FastAPI request path too, not just
    the Celery worker path where it was actually observed."""
    from app.core.idempotency import _client as idempotency_client
    from app.core.ratelimit import _client as ratelimit_client

    for build in (idempotency_client, ratelimit_client):
        kwargs = build().connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == settings.REDIS_SOCKET_TIMEOUT_SECONDS
        assert kwargs["socket_connect_timeout"] == settings.REDIS_SOCKET_TIMEOUT_SECONDS
