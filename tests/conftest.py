"""Shared pytest fixtures: testcontainers Postgres, fakeredis, httpx app client,
Celery task_always_eager. No shared dev database — see docs/conventions.md.
"""

import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from pathlib import Path

import boto3
import fakeredis.aioredis
import pytest
import pytest_asyncio
import structlog
from botocore.exceptions import ClientError
from httpx import ASGITransport, AsyncClient
from moto.server import ThreadedMotoServer
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from app.config import settings
from app.db.models import Base
from app.main import app


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:15-alpine", driver="asyncpg") as pg:
        yield pg


@pytest.fixture(scope="session", autouse=True)
def _moto_s3_server() -> Iterator[None]:
    """Integration tests upload real bytes. Before the S3 migration those
    bytes went to a live object-storage project, which needed credentials
    in CI and made the suite hostage to network blips — five unrelated tests
    failed that way in one week (see storage_service.py's docstring). A
    moto server is a real HTTP S3 endpoint, so presigned-URL round trips
    still work, with no external dependency.

    Session-scoped: one server for the run. Note tests/integration/ also
    shares a session-scoped Postgres container, and the same rule applies
    here — a test that mutates shared state must reset it on the way out.

    This is a *separate* server from tests/unit/test_storage_service.py's
    own per-test `s3` fixture, which spins up its own ThreadedMotoServer and
    monkeypatches settings.S3_ENDPOINT_URL for the duration of one test.
    That monkeypatch always reverts to whatever this fixture set at session
    start (never to None), and it also resets storage_service._client to
    None on the way out, so the next test's storage_service.get_client()
    call rebuilds a fresh client against this session server rather than
    reusing a client wired to the now-stopped per-test server.
    """
    mp = pytest.MonkeyPatch()
    # ThreadedMotoServer is a real HTTP server, so botocore's SigV4 signer
    # runs for real and needs *some* resolvable credentials — moto's backend
    # doesn't check their value. Session-scoped since this server itself is.
    mp.setenv("AWS_ACCESS_KEY_ID", "testing")
    mp.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"

    settings.S3_ENDPOINT_URL = endpoint
    settings.S3_REGION = "us-east-1"

    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    for bucket in (settings.BUCKET_INPUTS, settings.BUCKET_OUTPUTS):
        client.create_bucket(Bucket=bucket)

    yield

    server.stop()
    mp.undo()


@pytest_asyncio.fixture
async def db_engine(postgres_container: PostgresContainer) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(postgres_container.get_connection_url())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(
    db_engine: AsyncEngine, postgres_container: PostgresContainer, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # Phase 7: /generate now dispatches Celery tasks (orchestration.fan_out_job
    # -> generation.transform_photo) that, under task_always_eager, run inline
    # during the test — sometimes on a fresh thread+loop (app/workers/_async_utils.py),
    # so they can't share this fixture's connection pool (asyncpg connections
    # aren't shareable across event loops). Those workers build their own
    # engine per call from settings.DATABASE_URL read live — redirect that to
    # this test's container, same pattern as
    # tests/integration/test_migrations.py, so any cascaded task lands in the
    # test DB, never production.
    monkeypatch.setattr("app.config.settings.DATABASE_URL", postgres_container.get_connection_url())

    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[fakeredis.aioredis.FakeRedis, None]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def api_client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(autouse=True)
def _celery_eager() -> Iterator[None]:
    from app.workers.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


_logger = structlog.get_logger()

_GEMINI_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gemini"


@pytest.fixture(autouse=True)
def _fake_gemini_success_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 7: /generate now dispatches real generation work
    (orchestration.fan_out_job -> generation.transform_photo), which under
    task_always_eager runs inline during any test that calls /generate — see
    docs/ai-integration.md's "never call the live Gemini API in CI." Any test
    that wants a different outcome (failure, refusal, ...) monkeypatches
    GeminiProvider._call_api itself, same as before — a test-level patch
    always overrides this fixture-level default since it's applied later in
    the same test.
    """
    from app.providers.gemini import GeminiProvider

    fixture = json.loads((_GEMINI_FIXTURES / "success.json").read_text())
    monkeypatch.setattr(GeminiProvider, "_call_api", lambda self, *a, **k: fixture)


@contextmanager
def track_storage_uploads() -> Iterator[list[tuple[str, str]]]:
    """Patches storage_service.upload_bytes/upload_from_temp to record every
    (bucket, path) written while active, and deletes all of them on exit.
    Shared by `_cleanup_storage_uploads` below and by
    tests/integration/test_storage_cleanup_fixture.py's regression test, so
    the test exercises the exact same code the autouse fixture runs rather
    than a reimplementation that could drift from it.
    """
    from app.services import storage_service

    uploaded: list[tuple[str, str]] = []
    orig_upload_bytes = storage_service.upload_bytes
    orig_upload_from_temp = storage_service.upload_from_temp

    def tracked_upload_bytes(
        bucket: str, storage_path: str, data: bytes, content_type: str
    ) -> None:
        uploaded.append((bucket, storage_path))
        orig_upload_bytes(bucket, storage_path, data, content_type)

    def tracked_upload_from_temp(
        bucket: str, storage_path: str, local_path: Path, content_type: str
    ) -> None:
        uploaded.append((bucket, storage_path))
        orig_upload_from_temp(bucket, storage_path, local_path, content_type)

    storage_service.upload_bytes = tracked_upload_bytes  # type: ignore[assignment]
    storage_service.upload_from_temp = tracked_upload_from_temp  # type: ignore[assignment]
    try:
        yield uploaded
    finally:
        storage_service.upload_bytes = orig_upload_bytes  # type: ignore[assignment]
        storage_service.upload_from_temp = orig_upload_from_temp  # type: ignore[assignment]
        for bucket, path in uploaded:
            # Best-effort, but narrowly so: a unit test that mocks
            # storage_service.get_client for the duration of its own test
            # body (e.g. tests/unit/test_storage_service.py's `s3` fixture)
            # has that patch reverted by pytest's `monkeypatch` fixture
            # before this fixture's own teardown runs here, so `delete`
            # below can end up calling the real client, targeting a bucket
            # (e.g. "test-bucket") that only ever existed on that test's own
            # now-stopped per-test moto server, not the session-scoped one
            # tests/conftest.py::_moto_s3_server creates — a real S3 404
            # (NoSuchBucket/NoSuchKey), surfaced by boto3 as
            # botocore.exceptions.ClientError. That specific, expected case
            # is all this catches now.
            #
            # Until Task 6, every function in storage_service.py wasn't
            # boto3 yet (Tasks 2-5 migrated it function by function), so
            # this used to catch bare Exception to avoid a real crash from
            # whatever transitional shape `delete` happened to be in. As of
            # Task 6 the whole module is boto3, so a bug in this cleanup
            # logic itself -- not a real S3 error response -- should fail
            # loud again rather than log a warning and move on.
            try:
                storage_service.delete(bucket, path)
            except ClientError:
                _logger.warning(
                    "storage_cleanup_skipped", bucket=bucket, storage_path=path, exc_info=True
                )


@pytest.fixture(autouse=True)
def _cleanup_storage_uploads() -> Iterator[None]:
    """Every integration test that calls storage_service uploads real bytes
    to the session-scoped moto S3 server (`_moto_s3_server` above) — there
    is no per-test local Storage stub. The
    Postgres row a test creates alongside that upload lives in this test's
    ephemeral testcontainers DB and is gone at teardown; the Storage object
    is not, unless something removes it.

    Found during Phase 16's storage audit (docs/storage-audit-2026-08.md):
    39,618 of 39,656 objects in jewelry-outputs, and 17,404 of 17,449 in
    jewelry-inputs, had no matching `assets` row at all — accumulated test
    runs, not a production bug or code defect (every real, asset-backed
    object was 1:1 with its row). This fixture tracks every (bucket, path)
    a test uploads and removes it on teardown so the test suite stops
    growing the real, capacity-constrained bucket on every run.
    """
    with track_storage_uploads():
        yield


@pytest_asyncio.fixture(autouse=True)
async def _flush_rate_limit_keys() -> AsyncGenerator[None, None]:
    """Task 6 fix round — Important finding.

    This task's own moto-server migration (`_moto_s3_server` above) made the
    full integration suite ~10x faster (755s -> ~90s). That's a real,
    intended improvement, but it exposed a pre-existing, unrelated latent
    bug in test isolation: `app/services/rate_limiter.py` enforces a real
    fixed-window rate limit (`provider:gemini:tokens:{minute-window}`,
    capacity `GEMINI_RATE_LIMIT_PER_MINUTE`) against a REAL Redis instance
    (`settings.REDIS_URL` — not fakeredis; `app/core/ratelimit.py`'s
    per-client counter, `ratelimit:{client_id}:{minute}`, shares the exact
    same real-Redis wall-clock-window problem). Before this task, the slow
    (~12.5-minute) suite spread real-provider-classified calls across many
    real wall-clock minutes, so the shared per-minute budget rarely
    collided across unrelated tests. Now the whole suite finishes in ~90
    seconds — well inside one or two real minutes — so far more than 60
    provider-classified calls compete for one shared window, and tests
    later in the run genuinely got `RATE_LIMITED` and failed
    (`failure_class=RATE_LIMITED`, sub-job `FAILED` instead of
    `COMPLETED`). Reproduced twice independently (a different specific
    failing test each time — a real race, not a fixed bug).

    Fix: flush both key families **before** each test runs (not after) so
    every test starts with an isolated rate-limit window regardless of real
    wall-clock timing or what ran immediately before it in the same test
    session. Flushing before — not during/after — is what keeps
    `tests/integration/test_rate_limit_quota.py` able to deliberately
    exhaust its own window and see it enforced within its own test body:
    that test's own assertions run after this fixture's pre-test flush, so
    its first `/generate` call always starts from zero.

    Function-scoped and autouse for every test, not scoped to a directory
    or marker: a session-scoped flush would run once and not solve
    cross-test contention (the entire problem), and scoping this to
    "integration only" would require trusting that no other test file ever
    exercises `rate_limiter.acquire` or `ratelimit.allow` against real
    Redis — a narrower guarantee than "flush before every test," for no
    real benefit, since a test that never touches real Redis pays only the
    cost of one Redis round trip for a SCAN that matches nothing.

    Does NOT touch `GEMINI_RATE_LIMIT_PER_MINUTE` itself (stays 60) — the
    limiter's own real behavior, including genuinely hitting the cap within
    one test, must remain testable; see
    `tests/integration/test_rate_limit_quota.py`.

    Uses a real (not fake) Redis client against the same Redis instance the
    app connects to (`app.core.redis_client.new_redis_client`, which reads
    `settings.REDIS_URL` — see `.env`), matching how a human would flush
    with `docker exec jewellery-test-redis redis-cli` but via the same
    Python client the app itself uses rather than shelling out.
    """
    from app.core.redis_client import new_redis_client

    client = new_redis_client()
    try:
        for pattern in ("provider:gemini:tokens:*", "ratelimit:*"):
            keys = [key async for key in client.scan_iter(match=pattern)]
            if keys:
                await client.delete(*keys)
        yield
    finally:
        await client.aclose()


_QA_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "qa"


@pytest.fixture(autouse=True)
def _fake_qa_pass_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 9: a successful synthetic-angle generation now dispatches real
    QA scoring (app/workers/generation.py -> qa.score_similarity), which
    under task_always_eager runs inline during any test whose /generate call
    happens to include a synthetic angle — same cascade Phase 7 already hit
    for generation itself. Defaults to a high-similarity fixture so an
    existing test that doesn't care about QA scoring gets a plain COMPLETED
    sub-job rather than an unmocked network call (no real GEMINI_API_KEY
    exists in this environment). Tests that want a different QA outcome
    monkeypatch GeminiQaProvider._call_api themselves, same override rule as
    the generation fixture above.
    """
    from app.providers.gemini_qa import GeminiQaProvider

    fixture = json.loads((_QA_FIXTURES / "high_similarity.json").read_text())
    monkeypatch.setattr(GeminiQaProvider, "_call_api", lambda self, *a, **k: fixture)
