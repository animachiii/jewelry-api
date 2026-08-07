"""Phase 1 Checkpoint 2 — route existence, real auth, scopes, OpenAPI export.

Uses testcontainers Postgres (via db_session) seeded with just the api_clients
this checkpoint needs, and fakeredis for the health check's Redis ping.
"""

import json
import subprocess
import sys
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from openapi_spec_validator import validate
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.session import get_db
from app.main import app

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()

DOCUMENTED_ROUTES = {
    ("GET", "/api/v2/health"),
    ("GET", "/api/v2/config"),
    ("POST", "/api/v2/internal/config/sync"),
    ("POST", "/api/v2/uploads/presign"),
    ("POST", "/api/v2/generate"),
    ("GET", "/api/v2/status/{job_id}"),
    ("POST", "/api/v2/jobs/{job_id}/angles/{angle}/retry"),
    ("GET", "/api/v2/jobs"),
    ("GET", "/api/v2/jobs/{job_id}/cost"),
    ("GET", "/api/v2/qa/review-queue"),
    ("POST", "/api/v2/qa/{sub_job_id}/decision"),
}


def _make_key() -> tuple[str, str]:
    import secrets

    raw = secrets.token_urlsafe(32)
    return raw[:8], raw


@pytest.fixture
async def seeded_client_key(db_session: AsyncSession) -> str:
    _, raw = _make_key()
    client = ApiClient(
        name="test client",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope="client",
        is_active=True,
    )
    db_session.add(client)
    await db_session.commit()
    return raw


@pytest.fixture
async def seeded_ops_key(db_session: AsyncSession) -> str:
    _, raw = _make_key()
    client = ApiClient(
        name="test ops",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope="ops",
        is_active=True,
    )
    db_session.add(client)
    await db_session.commit()
    return raw


@pytest.fixture
async def seeded_revoked_key(db_session: AsyncSession) -> str:
    _, raw = _make_key()
    client = ApiClient(
        name="test revoked",
        key_prefix=raw[:8],
        key_hash=_hasher.hash(raw),
        scope="client",
        is_active=False,
        revoked_at=datetime.now(UTC),
    )
    db_session.add(client)
    await db_session.commit()
    return raw


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def test_every_documented_route_exists() -> None:
    schema = app.openapi()
    actual: set[tuple[str, str]] = set()
    for path, path_item in schema["paths"].items():
        for method in path_item:
            if method in ("get", "post", "put", "delete", "patch"):
                actual.add((method.upper(), path))
    assert actual >= DOCUMENTED_ROUTES


async def test_health_requires_no_api_key(client: AsyncClient) -> None:
    resp = await client.get("/api/v2/health")
    assert resp.status_code in (200, 503)


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-API-Key": "not-a-real-key"}],
)
async def test_protected_route_without_or_with_malformed_key_401(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    resp = await client.get("/api/v2/config", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_API_KEY"


async def test_revoked_client_key_401(client: AsyncClient, seeded_revoked_key: str) -> None:
    resp = await client.get("/api/v2/config", headers={"X-API-Key": seeded_revoked_key})
    assert resp.status_code == 401


async def test_client_scope_key_403_on_ops_routes(
    client: AsyncClient, seeded_client_key: str
) -> None:
    resp = await client.get("/api/v2/jobs", headers={"X-API-Key": seeded_client_key})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "INSUFFICIENT_SCOPE"

    resp2 = await client.get("/api/v2/qa/review-queue", headers={"X-API-Key": seeded_client_key})
    assert resp2.status_code == 403


async def test_ops_scope_key_succeeds_past_auth_on_ops_routes(
    client: AsyncClient, seeded_ops_key: str
) -> None:
    resp = await client.get("/api/v2/jobs", headers={"X-API-Key": seeded_ops_key})
    # Auth/scope passed; NotImplementedError surfaces as 500 until later phases.
    assert resp.status_code != 401
    assert resp.status_code != 403


async def test_openapi_spec_has_x_api_key_security_except_health() -> None:
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["ApiKeyAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    assert schema["paths"]["/api/v2/health"]["get"].get("security") == []
    assert schema["security"] == [{"ApiKeyAuth": []}]


def test_every_route_documents_error_responses() -> None:
    schema = app.openapi()
    for method, path in DOCUMENTED_ROUTES:
        operation = schema["paths"][path][method.lower()]
        non_2xx = [code for code in operation["responses"] if not code.startswith("2")]
        assert non_2xx, f"{method} {path} documents no error responses"


def test_openapi_export_script_writes_committed_file(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/export_openapi.py"],
        cwd=Path(__file__).resolve().parent.parent.parent,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    committed_path = Path(__file__).resolve().parent.parent.parent / "docs" / "openapi.json"
    assert committed_path.exists()
    schema = json.loads(committed_path.read_text())
    validate(schema)
    assert schema["openapi"].startswith("3.1")
