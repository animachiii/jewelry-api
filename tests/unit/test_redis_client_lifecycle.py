"""app/core/redis_client.py's two contracts.

The singleton is correct for FastAPI (one process-long event loop) and fatal
for Celery tasks, whose `asyncio.run()` closes the loop after every task --
leaving the cached client's connections bound to a dead loop, so the next
task in that worker process raises `RuntimeError('Event loop is closed')`.
Third occurrence of this exact bug in this codebase; see
`new_redis_client`'s docstring.
"""

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
    import app.workers.config as config_worker
    import app.workers.generation as generation_worker

    for module in (config_worker, generation_worker):
        assert not hasattr(module, "get_redis_client"), (
            f"{module.__name__} must use new_redis_client(), not the shared singleton"
        )
        assert hasattr(module, "new_redis_client")
