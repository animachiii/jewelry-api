"""Shared pytest fixtures: testcontainers Postgres, fakeredis, httpx app client,
Celery task_always_eager. No shared dev database — see docs/conventions.md.
"""

import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from pathlib import Path

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer

from app.db.models import Base
from app.main import app


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:15-alpine", driver="asyncpg") as pg:
        yield pg


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
            storage_service.delete(bucket, path)


@pytest.fixture(autouse=True)
def _cleanup_storage_uploads() -> Iterator[None]:
    """Every integration test that calls storage_service uploads real bytes
    to the real, shared Supabase project — there is no local Storage stub
    (docs/ai-integration.md: Storage is deliberately never mocked). The
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
