"""Phase 15 Steps 4-5 — POST /background/remove, POST /background/replace,
operation-aware presign, GET /status/{job_id}'s additive `operation`/
`results` fields, POST /jobs/{job_id}/retry, and the real
background.process worker + subject-preservation QA gate.

Same stack as tests/integration/test_generate_real.py /
tests/integration/test_qa_gate.py: testcontainers Postgres, real local
Redis (idempotency, rate limiting), real Supabase Storage (never mocked),
fixture-driven Gemini (tests/conftest.py's autouse
`_fake_gemini_success_by_default` / `_fake_qa_pass_by_default`). Under
`task_always_eager` (also autouse), `background.process` and
`qa.score_background` both run inline during a single request — a
background job created here runs to a real terminal status before the
201/202 response's own DB reads happen, not "sits at PENDING."
"""

import io
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.assets import Asset
from app.db.models.config_versions import ConfigVersion
from app.db.models.cost_events import CostEvent
from app.db.models.enums import (
    AssetKind,
    FailureClass,
    JobStatus,
    Operation,
    QAStatus,
    SourceType,
    SubJobStatus,
    SyncStatus,
)
from app.db.models.jobs import Job, SubJob
from app.db.session import get_db
from app.main import app
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()
_GEMINI_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"
_QA_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "qa"


def _load(base: Path, name: str) -> dict:
    return json.loads((base / name).read_text())


def _fake_gemini_generation(monkeypatch: pytest.MonkeyPatch, fixture_name: str) -> None:
    import app.providers.gemini as gemini_module

    fixture = _load(_GEMINI_FIXTURES, fixture_name)
    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", lambda self, *a, **k: fixture)


def _fake_qa_score(monkeypatch: pytest.MonkeyPatch, fixture_name: str) -> None:
    import app.providers.gemini_qa as qa_module

    fixture = _load(_QA_FIXTURES, fixture_name)
    monkeypatch.setattr(qa_module.GeminiQaProvider, "_call_api", lambda self, *a, **k: fixture)


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _make_client(
    db_session: AsyncSession, name: str, scope: str = "client"
) -> tuple[ApiClient, str]:
    import secrets

    raw = secrets.token_urlsafe(32)
    api_client = ApiClient(
        name=name, key_prefix=raw[:8], key_hash=_hasher.hash(raw), scope=scope, is_active=True
    )
    db_session.add(api_client)
    await db_session.flush()
    return api_client, raw


@pytest.fixture
async def active_config(db_session: AsyncSession) -> ConfigVersion:
    cv = ConfigVersion(
        version_number=1,
        source_hash="test-hash",
        payload=CATEGORY_PAYLOAD,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


@pytest.fixture
async def ops_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "background-test-ops", scope="ops")
    await db_session.commit()
    return raw


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "background-test-client")
    await db_session.commit()
    return raw


def _real_jpeg_bytes(size: tuple[int, int] = (32, 24)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 200, 10)).save(buf, format="JPEG")
    return buf.getvalue()


async def _presign_and_upload_operation(client: AsyncClient, key: str, operation: str) -> str:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"operation": operation},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["angles"] == []
    upload = body["operation_upload"]
    assert upload["operation"] == operation
    put_resp = httpx.put(
        upload["upload_url"], content=_real_jpeg_bytes(), headers={"Content-Type": "image/jpeg"}
    )
    assert put_resp.status_code == 200
    return str(upload["storage_path"])


# --- presign -----------------------------------------------------------


async def test_presign_operation_mode_rejects_mixing_with_category(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"operation": "BACKGROUND_REMOVAL", "category_code": "RING", "angles": ["FRONT"]},
    )
    assert resp.status_code == 422, resp.text


async def test_presign_rejects_angle_generation_as_operation(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"operation": "ANGLE_GENERATION"},
    )
    assert resp.status_code == 422, resp.text


async def test_presign_operation_mode_includes_background_upload_when_requested(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"operation": "BACKGROUND_REPLACEMENT", "include_background_upload": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["background_upload"] is not None
    assert body["background_upload"]["storage_path"] != body["operation_upload"]["storage_path"]


async def test_presign_omits_background_upload_by_default(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"operation": "BACKGROUND_REPLACEMENT"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["background_upload"] is None


async def test_presign_background_upload_rejected_for_removal_operation(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"operation": "BACKGROUND_REMOVAL", "include_background_upload": True},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "include_background_upload is only valid with" in body["error"]["message"]


# --- POST /background/remove --------------------------------------------


async def test_remove_background_happy_path(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """The 202 response reflects the plan at accept time (PENDING) — under
    `task_always_eager` (tests/conftest.py), `background.process` and then
    `qa.score_background` both run inline before this call returns, so a
    DB read afterward sees the real terminal state, not PENDING. Real
    round trip end to end: submit -> generate (fixture-driven Gemini,
    autouse `_fake_gemini_success_by_default`) -> QA_REVIEW -> scored
    (autouse `_fake_qa_pass_by_default`, 0.94 clears the 0.92
    background_qa_similarity_threshold) -> COMPLETED. See
    phases/phase-15-background-operations.md Step 5.
    """
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")

    resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "remove-1"},
        json={"storage_path": storage_path, "sku_reference": "SKU-1"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["angles"] == [
        {
            "angle": None,
            "source_type": "UPLOADED",
            "status": "PENDING",
            "storage_path": storage_path,
        }
    ]

    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(body["job_id"])))
    ).scalar_one()
    assert job.operation == Operation.BACKGROUND_REMOVAL
    assert job.category_code is None
    assert job.requested_angles == 1
    assert job.sku_reference == "SKU-1"
    assert job.status == JobStatus.COMPLETED
    assert job.succeeded_angles == 1

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 1
    sub_job = sub_jobs[0]
    assert sub_job.angle is None
    assert sub_job.source_type == SourceType.UPLOADED
    assert sub_job.status == SubJobStatus.COMPLETED
    assert sub_job.qa_status == QAStatus.PASSED
    assert float(sub_job.qa_score) == pytest.approx(0.94)
    assert sub_job.output_asset_id is not None
    assert sub_job.model_version is not None

    cost_events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == job.id)))
        .scalars()
        .all()
    )
    assert len(cost_events) == 1
    assert cost_events[0].operation == "background_removal"
    assert float(cost_events[0].unit_cost_usd) == pytest.approx(0.02)


async def test_remove_background_requires_idempotency_key(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key},
        json={"storage_path": "pending/x/y/BACKGROUND_REMOVAL/input_x.jpg"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


async def test_remove_background_requires_api_key(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v2/background/remove",
        json={"storage_path": "pending/x/y/BACKGROUND_REMOVAL/input_x.jpg"},
    )
    assert resp.status_code == 401, resp.text


async def test_remove_background_replay_creates_no_second_job(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    payload = {"storage_path": storage_path}
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "remove-replay-1"}

    first = await client.post("/api/v2/background/remove", headers=headers, json=payload)
    second = await client.post("/api/v2/background/remove", headers=headers, json=payload)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["job_id"] == second.json()["job_id"]

    jobs = (
        (await db_session.execute(select(Job).where(Job.operation == Operation.BACKGROUND_REMOVAL)))
        .scalars()
        .all()
    )
    assert len(jobs) == 1


async def test_remove_background_same_key_different_payload_409(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "remove-conflict-1"}

    first = await client.post(
        "/api/v2/background/remove", headers=headers, json={"storage_path": storage_path}
    )
    assert first.status_code == 202, first.text

    second = await client.post(
        "/api/v2/background/remove",
        headers=headers,
        json={"storage_path": storage_path, "sku_reference": "different-payload"},
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


async def test_remove_background_storage_path_owned_by_other_client(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession, active_config: ConfigVersion
) -> None:
    """Deviation from the phase file's literal Checkpoint 4 wording ("returns
    404, never 403"): this reuses job_service's existing AssetNotOwnedError
    (422), the same status/code POST /generate already uses for identical
    ownership violations (docs/api-routes.md validation rule 4). Treated as
    a copy/paste artifact from GET /status's job-scoping rule (a genuinely
    different concern — hiding whether a resource *exists* from a
    non-owner), not a deliberate divergence from established precedent.
    """
    other_client, other_key = await _make_client(db_session, "other-background-client")
    await db_session.commit()
    storage_path = await _presign_and_upload_operation(client, other_key, "BACKGROUND_REMOVAL")

    resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "remove-other-1"},
        json={"storage_path": storage_path},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ASSET_NOT_OWNED"


async def _presign_and_upload_replacement_with_custom_background(
    client: AsyncClient, key: str
) -> tuple[str, str]:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"operation": "BACKGROUND_REPLACEMENT", "include_background_upload": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    product_upload = body["operation_upload"]
    background_upload = body["background_upload"]
    assert background_upload is not None

    put_resp = httpx.put(
        product_upload["upload_url"],
        content=_real_jpeg_bytes(),
        headers={"Content-Type": "image/jpeg"},
    )
    assert put_resp.status_code == 200
    bg_put_resp = httpx.put(
        background_upload["upload_url"],
        content=_real_jpeg_bytes(size=(48, 32)),
        headers={"Content-Type": "image/jpeg"},
    )
    assert bg_put_resp.status_code == 200
    return str(product_upload["storage_path"]), str(background_upload["storage_path"])


async def test_replace_background_custom_background_owned_by_other_client(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    _, other_key = await _make_client(db_session, "other-bg-custom-client")
    await db_session.commit()
    storage_path = await _presign_and_upload_operation(
        client, api_client_key, "BACKGROUND_REPLACEMENT"
    )
    _, other_background_path = await _presign_and_upload_replacement_with_custom_background(
        client, other_key
    )

    resp = await client.post(
        "/api/v2/background/replace",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "replace-bg-other-1"},
        json={"storage_path": storage_path, "background_storage_path": other_background_path},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ASSET_NOT_OWNED"


async def test_replace_background_custom_background_creates_and_links_asset(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """Service-layer proof, independent of the worker: create_background_job_for_request
    (Task 6) itself validates, persists, and links the uploaded background photo —
    this must hold even before the worker (Task 7) does anything with it. Under
    task_always_eager the job still runs to completion here since
    background_service.process doesn't look at background_asset_id yet (confirmed
    separately) — it just ignores it and produces a normal single-image result,
    which is why this test can assert COMPLETED the same way the preset-based
    happy path does, without needing Task 7's changes.
    """
    (
        storage_path,
        background_storage_path,
    ) = await _presign_and_upload_replacement_with_custom_background(client, api_client_key)

    resp = await client.post(
        "/api/v2/background/replace",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "replace-custom-bg-link-1"},
        json={"storage_path": storage_path, "background_storage_path": background_storage_path},
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.operation == Operation.BACKGROUND_REPLACEMENT
    assert job.preset_code is None
    assert job.status == JobStatus.COMPLETED

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.background_asset_id is not None

    background_asset = (
        await db_session.execute(select(Asset).where(Asset.id == sub_job.background_asset_id))
    ).scalar_one()
    assert background_asset.job_id == job.id
    assert background_asset.kind == AssetKind.INPUT
    assert background_asset.storage_path == background_storage_path
    assert background_asset.expires_at is not None  # retention_policy applied here too


async def test_replace_background_custom_background_happy_path(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom-background compositing: two images (product + background) go
    into one Gemini call, one cost event, one output. See
    docs/superpowers/specs/2026-08-13-custom-background-compositing-design.md.
    """
    import app.providers.gemini as gemini_module

    captured: dict = {}
    fixture = _load(_GEMINI_FIXTURES, "success.json")

    def _capture_call(self: object, prompt: str, reference_images: list, seed: int) -> dict:
        captured["prompt"] = prompt
        captured["reference_image_count"] = len(reference_images)
        captured["reference_images"] = list(reference_images)
        return fixture

    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", _capture_call)

    (
        storage_path,
        background_storage_path,
    ) = await _presign_and_upload_replacement_with_custom_background(client, api_client_key)

    resp = await client.post(
        "/api/v2/background/replace",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "replace-custom-bg-happy-1"},
        json={"storage_path": storage_path, "background_storage_path": background_storage_path},
    )
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    assert captured["reference_image_count"] == 2
    assert "naturally into the supplied background photo" in captured["prompt"]
    # Not just a count: confirm which image actually landed in which slot.
    # The product/background fixtures are deliberately different sizes
    # (32x24 vs 48x32, see _presign_and_upload_replacement_with_custom_background)
    # so decoding both catches an accidental order swap or a wrong asset
    # being downloaded, not just "two images arrived."
    product_image = Image.open(io.BytesIO(captured["reference_images"][0]))
    background_image = Image.open(io.BytesIO(captured["reference_images"][1]))
    assert product_image.size == (32, 24)
    assert background_image.size == (48, 32)

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.operation == Operation.BACKGROUND_REPLACEMENT
    assert job.preset_code is None
    assert job.status == JobStatus.COMPLETED

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.background_asset_id is not None
    assert sub_job.status == SubJobStatus.COMPLETED

    cost_events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == job_id)))
        .scalars()
        .all()
    )
    assert len(cost_events) == 1  # still one Gemini call, regardless of image count


async def test_remove_background_disabled_operation_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = dict(CATEGORY_PAYLOAD)
    payload["global"] = dict(payload["global"])
    payload["global"]["operations"] = {
        "BACKGROUND_REMOVAL": {"enabled": False},
        "BACKGROUND_REPLACEMENT": {"enabled": True},
    }
    cv = ConfigVersion(
        version_number=1,
        source_hash="disabled-op-hash",
        payload=payload,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    _, key = await _make_client(db_session, "disabled-op-client")
    await db_session.commit()

    storage_path = await _presign_and_upload_operation(client, key, "BACKGROUND_REMOVAL")
    resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": key, "Idempotency-Key": "disabled-op-1"},
        json={"storage_path": storage_path},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "OPERATION_DISABLED"


# --- POST /background/replace -------------------------------------------


async def test_replace_background_happy_path(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    storage_path = await _presign_and_upload_operation(
        client, api_client_key, "BACKGROUND_REPLACEMENT"
    )

    resp = await client.post(
        "/api/v2/background/replace",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "replace-1"},
        json={"storage_path": storage_path, "preset_code": "STUDIO_WHITE"},
    )
    assert resp.status_code == 202, resp.text

    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(resp.json()["job_id"])))
    ).scalar_one()
    assert job.operation == Operation.BACKGROUND_REPLACEMENT


async def test_replace_background_unknown_preset_code_422(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_operation(
        client, api_client_key, "BACKGROUND_REPLACEMENT"
    )
    resp = await client.post(
        "/api/v2/background/replace",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "replace-unknown-1"},
        json={"storage_path": storage_path, "preset_code": "NO_SUCH_PRESET"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "PRESET_NOT_FOUND"


async def test_replace_background_inactive_preset_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = dict(CATEGORY_PAYLOAD)
    payload["global"] = dict(payload["global"])
    payload["global"]["background_presets"] = [
        {
            "code": "RETIRED_BACKDROP",
            "name": "Retired",
            "prompt": "n/a",
            "reference_image_urls": [],
            "is_active": False,
        }
    ]
    cv = ConfigVersion(
        version_number=1,
        source_hash="inactive-preset-hash",
        payload=payload,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    _, key = await _make_client(db_session, "inactive-preset-client")
    await db_session.commit()

    storage_path = await _presign_and_upload_operation(client, key, "BACKGROUND_REPLACEMENT")
    resp = await client.post(
        "/api/v2/background/replace",
        headers={"X-API-Key": key, "Idempotency-Key": "inactive-preset-1"},
        json={"storage_path": storage_path, "preset_code": "RETIRED_BACKDROP"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "PRESET_INACTIVE"


async def test_replace_background_rejects_both_preset_and_custom_background(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_operation(
        client, api_client_key, "BACKGROUND_REPLACEMENT"
    )
    resp = await client.post(
        "/api/v2/background/replace",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "replace-both-1"},
        json={
            "storage_path": storage_path,
            "preset_code": "STUDIO_WHITE",
            "background_storage_path": "pending/x/y/z/background_1.jpg",
        },
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "exactly one of preset_code or background_storage_path" in body["error"]["message"]


async def test_replace_background_rejects_neither_preset_nor_custom_background(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_operation(
        client, api_client_key, "BACKGROUND_REPLACEMENT"
    )
    resp = await client.post(
        "/api/v2/background/replace",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "replace-neither-1"},
        json={"storage_path": storage_path},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "exactly one of preset_code or background_storage_path" in body["error"]["message"]


# --- GET /status/{job_id} -------------------------------------------------


async def test_status_for_background_job_returns_operation_and_results(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "status-remove-1"},
        json={"storage_path": storage_path},
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.get(f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "BACKGROUND_REMOVAL"
    assert body["angles"] == []
    assert len(body["results"]) == 1
    result = body["results"][0]
    # Under task_always_eager the job has already run to completion by the
    # time this GET happens — see this file's module docstring.
    assert result["status"] == "COMPLETED"
    assert result["source_type"] == "UPLOADED"
    assert result["qa_status"] == "PASSED"
    assert result["image_url"] is not None
    assert "angle" not in result


async def test_status_for_angle_job_is_additive_only(
    client: AsyncClient, api_client_key: str
) -> None:
    """Pins the Checkpoint 4 claim: an angle job's `GET /status` response
    gains only `operation` and `results` (empty) relative to every field
    Phase 1-9's own status tests already assert — not re-deriving a
    pre-phase fixture file (none exists in this repo to diff against), but
    asserting the additive shape directly.
    """
    presign = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"category_code": "RING", "angles": ["FRONT"]},
    )
    presigned = presign.json()["angles"][0]
    httpx.put(
        presigned["upload_url"], content=_real_jpeg_bytes(), headers={"Content-Type": "image/jpeg"}
    )
    create_resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "status-angle-1"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": presigned["storage_path"]},
                "SIDE": {"skip": True},
                "DIAGONAL": {"skip": True},
                "TOP": {"skip": True},
            },
        },
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.get(f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "ANGLE_GENERATION"
    assert body["results"] == []
    assert len(body["angles"]) == 4
    assert body["angles"][0]["angle"] is not None


# --- POST /jobs/{job_id}/retry --------------------------------------------


async def test_retry_job_409_on_angle_job(client: AsyncClient, api_client_key: str) -> None:
    presign = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"category_code": "RING", "angles": ["FRONT"]},
    )
    presigned = presign.json()["angles"][0]
    httpx.put(
        presigned["upload_url"], content=_real_jpeg_bytes(), headers={"Content-Type": "image/jpeg"}
    )
    create_resp = await client.post(
        "/api/v2/generate",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-angle-job-1"},
        json={
            "category_code": "RING",
            "angles": {
                "FRONT": {"storage_path": presigned["storage_path"]},
                "SIDE": {"skip": True},
                "DIAGONAL": {"skip": True},
                "TOP": {"skip": True},
            },
        },
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-angle-job-attempt-1"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "ANGLE_JOB_RETRY_NOT_ALLOWED"


async def test_retry_job_404_for_unknown_job(client: AsyncClient, api_client_key: str) -> None:
    resp = await client.post(
        f"/api/v2/jobs/{uuid.uuid4()}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-unknown-1"},
    )
    assert resp.status_code == 404, resp.text


async def test_retry_job_202_on_failed_background_job_runs_to_completion(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """Forces a completed background job's sub-job back to FAILED (simulating
    the outcome of a real provider failure, which fixture-driven success
    never produces on its own — same technique
    tests/integration/test_retry_real.py uses), then retries it. Under
    task_always_eager the retried background.process call runs inline
    before this request returns, so — like
    test_remove_background_happy_path — the real assertion is the final
    terminal state, not an intermediate PENDING.
    """
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-bg-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status == SubJobStatus.COMPLETED  # real completion from the initial call
    attempt_count_before = sub_job.attempt_count
    sub_job.status = SubJobStatus.FAILED
    sub_job.failure_class = FailureClass.TRANSIENT_NETWORK
    job.status = JobStatus.FAILED
    await db_session.commit()

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-bg-attempt-1"},
    )
    assert resp.status_code == 202, resp.text

    await db_session.refresh(sub_job)
    await db_session.refresh(job)
    assert sub_job.status == SubJobStatus.COMPLETED
    assert sub_job.attempt_count > attempt_count_before
    assert job.status == JobStatus.COMPLETED


async def test_retry_job_409_when_not_failed(client: AsyncClient, api_client_key: str) -> None:
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-bg-pending-1"},
        json={"storage_path": storage_path},
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-bg-pending-attempt-1"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "SUBJOB_NOT_RETRYABLE"


async def test_retry_job_replay_same_key_is_noop_for_background(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """Regression for the job-level retry-idempotency-target redesign
    (app/api/v2/retry.py::retry_job) — Phase 18 Step 4 re-keyed the stored
    idempotency target from the single sub-job's own ID (`f"{sub_job.id}"`)
    to `f"{job.id}:retry"`, to generalize the route to a variable number of
    sub-jobs (MATCH). This must be a strict behavior-preserving no-op for
    the pre-existing single-sub-job (background) case: a replay of the same
    Idempotency-Key against a job-level retry must still return a no-op 202,
    not re-dispatch background.process a second time. Same shape as
    tests/integration/test_retry_real.py's
    test_retry_replay_same_key_is_noop_no_second_dispatch, which pins this
    for the angle-level route (unchanged by this redesign).
    """
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "retry-bg-replay-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status == SubJobStatus.COMPLETED  # real completion from the initial call
    sub_job.status = SubJobStatus.FAILED
    sub_job.failure_class = FailureClass.TRANSIENT_NETWORK
    job.status = JobStatus.FAILED
    await db_session.commit()

    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "retry-bg-replay-attempt-1"}
    first = await client.post(f"/api/v2/jobs/{job_id}/retry", headers=headers)
    assert first.status_code == 202, first.text

    await db_session.refresh(sub_job)
    attempt_count_after_first = sub_job.attempt_count
    assert sub_job.status == SubJobStatus.COMPLETED

    second = await client.post(f"/api/v2/jobs/{job_id}/retry", headers=headers)
    assert second.status_code == 202, second.text

    await db_session.refresh(sub_job)
    assert sub_job.attempt_count == attempt_count_after_first  # no second dispatch happened


# --- Step 5: worker, provider failure classes, subject-preservation QA gate --


async def test_round_trip_output_downloads_as_a_real_png(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """Checkpoint 5: "Round trip: submit -> poll -> download the output ->
    it opens as a valid image, asserted on magic bytes, not on the sub-job
    saying COMPLETED."
    """
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "round-trip-1"},
        json={"storage_path": storage_path},
    )
    job_id = create_resp.json()["job_id"]

    status_resp = await client.get(
        f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key}
    )
    assert status_resp.status_code == 200, status_resp.text
    result = status_resp.json()["results"][0]
    assert result["status"] == "COMPLETED"
    image_url = result["image_url"]
    assert image_url is not None

    downloaded = httpx.get(image_url)
    assert downloaded.status_code == 200
    assert (
        downloaded.content[:8] == b"\x89PNG\r\n\x1a\n"
    )  # PNG magic bytes, not just a status string
    Image.open(io.BytesIO(downloaded.content)).verify()  # actually decodes


async def test_below_threshold_qa_score_lands_in_qa_review_and_review_queue(
    client: AsyncClient,
    api_client_key: str,
    ops_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_qa_score(monkeypatch, "low_similarity.json")

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-flag-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status == SubJobStatus.QA_REVIEW
    assert sub_job.qa_status == QAStatus.FLAGGED
    assert float(sub_job.qa_score) == pytest.approx(0.41)

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status == JobStatus.PROCESSING

    queue_resp = await client.get("/api/v2/qa/review-queue", headers={"X-API-Key": ops_key})
    assert queue_resp.status_code == 200, queue_resp.text
    assert str(sub_job.id) in {item["sub_job_id"] for item in queue_resp.json()["items"]}


async def test_qa_provider_failure_flags_for_review_never_completed(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_qa_score(monkeypatch, "malformed.json")

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-provider-error-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status == SubJobStatus.QA_REVIEW
    assert sub_job.qa_status == QAStatus.FLAGGED
    assert sub_job.qa_score is None


async def test_status_shows_preview_image_and_retry_url_while_flagged(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/superpowers/specs/2026-08-13-background-qa-preview-and-retry-design.md:
    a flagged job is no longer a dead end for the client — the generated
    image is visible via preview_image_url (image_url itself stays exactly
    as documented, COMPLETED-only) and retry_url is offered.
    """
    _fake_qa_score(monkeypatch, "low_similarity.json")

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-preview-1"},
        json={"storage_path": storage_path},
    )
    job_id = create_resp.json()["job_id"]

    status_resp = await client.get(
        f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key}
    )
    assert status_resp.status_code == 200, status_resp.text
    result = status_resp.json()["results"][0]
    assert result["status"] == "QA_REVIEW"
    assert result["qa_status"] == "FLAGGED"
    assert result["image_url"] is None  # unchanged: never shown before human/retry
    assert result["preview_image_url"] is not None
    assert result["retryable"] is True
    assert result["retry_url"] == f"/api/v2/jobs/{job_id}/retry"


async def test_retry_qa_only_completes_without_regenerating(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real 2026-08-13 incident this feature exists for: generation
    succeeded, only the judge call failed/scored low. Retrying must re-run
    just the judge (qa.score_background) against the *same* output image —
    not app/workers/background.process, which would burn a second billed
    Gemini call and could even produce a different image.
    """
    _fake_qa_score(monkeypatch, "low_similarity.json")

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-retry-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status == SubJobStatus.QA_REVIEW
    assert sub_job.qa_status == QAStatus.FLAGGED
    output_asset_id_before = sub_job.output_asset_id
    attempt_count_before = sub_job.attempt_count
    generation_cost_events_before = (
        await db_session.execute(select(CostEvent).where(CostEvent.job_id == job_id))
    ).all()

    _fake_qa_score(monkeypatch, "high_similarity.json")

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-retry-attempt-1"},
    )
    assert retry_resp.status_code == 202, retry_resp.text

    await db_session.refresh(sub_job)
    assert sub_job.status == SubJobStatus.COMPLETED
    assert sub_job.qa_status == QAStatus.PASSED
    assert float(sub_job.qa_score) == pytest.approx(0.94)
    assert sub_job.attempt_count > attempt_count_before
    # Same image — the judge re-ran, generation did not.
    assert sub_job.output_asset_id == output_asset_id_before

    generation_cost_events_after = (
        await db_session.execute(select(CostEvent).where(CostEvent.job_id == job_id))
    ).all()
    assert len(generation_cost_events_after) == len(generation_cost_events_before)

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status == JobStatus.COMPLETED

    status_resp = await client.get(
        f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key}
    )
    result = status_resp.json()["results"][0]
    assert result["image_url"] is not None  # now COMPLETED, the real field populates too


async def test_retry_qa_only_on_provider_error_flag_also_works(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qa_score: NULL (the judge call itself failed, not a low score) must
    be just as retryable — this is the exact shape of the real 2026-08-13
    incident that motivated this feature."""
    _fake_qa_score(monkeypatch, "malformed.json")

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-retry-provider-error-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.qa_score is None

    _fake_qa_score(monkeypatch, "high_similarity.json")
    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={
            "X-API-Key": api_client_key,
            "Idempotency-Key": "qa-retry-provider-error-attempt-1",
        },
    )
    assert retry_resp.status_code == 202, retry_resp.text

    await db_session.refresh(sub_job)
    assert sub_job.status == SubJobStatus.COMPLETED
    assert float(sub_job.qa_score) == pytest.approx(0.94)


async def test_retry_qa_only_409_when_not_flagged(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """A COMPLETED job (the fixture-default happy path) is not QA-retryable
    — falls through to the existing FAILED-only check, unchanged."""
    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-retry-not-flagged-1"},
        json={"storage_path": storage_path},
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-retry-not-flagged-attempt-1"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "SUBJOB_NOT_RETRYABLE"


async def test_safety_refusal_rejected_no_retry(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_gemini_generation(monkeypatch, "safety_refusal.json")

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "safety-refusal-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status == SubJobStatus.REJECTED
    assert sub_job.failure_class == FailureClass.SAFETY_REFUSAL
    assert sub_job.attempt_count == 1

    status_resp = await client.get(
        f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key}
    )
    result = status_resp.json()["results"][0]
    assert result["retryable"] is False
    assert result["retry_url"] is None

    cost_events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == job_id)))
        .scalars()
        .all()
    )
    assert len(cost_events) == 1  # a refusal is still billed — docs/business-rules.md §10


async def test_transient_provider_failure_exhausts_attempts_then_fails(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers.gemini as gemini_module

    def _raise_503(self: object, *a: object, **k: object) -> dict:
        raise gemini_module.GeminiAPIError(503, "The model is overloaded.")

    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", _raise_503)

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "transient-exhaust-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status == SubJobStatus.FAILED
    assert sub_job.failure_class == FailureClass.TRANSIENT_PROVIDER
    assert sub_job.attempt_count == 3  # background_service.MAX_ATTEMPTS

    # Exactly one cost_events row per provider call — docs/business-rules.md §10.
    cost_events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == job_id)))
        .scalars()
        .all()
    )
    assert len(cost_events) == 3
    assert all(e.operation == "background_removal" for e in cost_events)


async def test_provider_timeout_classifies_transient_network(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers.gemini as gemini_module

    def _raise_timeout(self: object, *a: object, **k: object) -> dict:
        raise TimeoutError("Gemini request timed out.")

    monkeypatch.setattr(gemini_module.GeminiProvider, "_call_api", _raise_timeout)

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "timeout-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    sub_job = (await db_session.execute(select(SubJob).where(SubJob.job_id == job_id))).scalar_one()
    assert sub_job.status == SubJobStatus.FAILED
    assert sub_job.failure_class == FailureClass.TRANSIENT_NETWORK


async def test_flagged_background_job_records_the_judge_s_reasoning(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Added 2026-08-28. Before this, a flagged background operation left a
    QA_SCORED event carrying only a score and a threshold, so "why was this
    flagged?" could only be answered by re-downloading both images and
    re-running the judge by hand — which is exactly what the client-reported
    recurrence cost. The judge's own reasoning is now on the event.
    """
    from app.db.models.job_events import JobEvent

    _fake_qa_score(monkeypatch, "low_similarity.json")

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-reasoning-1"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    event = (
        await db_session.execute(
            select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.event_type == "QA_SCORED")
        )
    ).scalar_one()

    assert event.detail["outcome"] == "flagged"
    assert event.detail["reasoning"] == "Prong count differs from every reference image."


async def test_provider_error_records_the_failure_class_as_reasoning(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NULL qa_score is the least self-explanatory outcome of the three —
    it is what 17 stale review-queue rows looked like after the 2026-08-21
    parse bug. The event now says which failure class caused it.
    """
    from app.db.models.job_events import JobEvent

    _fake_qa_score(monkeypatch, "malformed.json")

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    create_resp = await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-reasoning-2"},
        json={"storage_path": storage_path},
    )
    job_id = uuid.UUID(create_resp.json()["job_id"])

    event = (
        await db_session.execute(
            select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.event_type == "QA_SCORED")
        )
    ).scalar_one()

    assert event.detail["outcome"] == "provider_error"
    assert "score" not in event.detail
    assert event.detail["reasoning"].startswith("INTERNAL: ")


async def test_background_scoring_asks_the_subject_preservation_question(
    client: AsyncClient,
    api_client_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this whole change exists for: a background operation
    must not be judged by the synthetic-angle piece-identity prompt. A
    correct BACKGROUND_REMOVAL output legitimately differs from its input in
    background, props, crop and pose, and the piece-identity judge scores
    exactly those differences as "a materially different piece" (live
    sub-job 6b3eda1e, 2026-08-27, scored 0.0 on a flawless output).
    """
    import app.providers.gemini_qa as qa_module

    fixture = _load(_QA_FIXTURES, "high_similarity.json")
    seen: list[str] = []

    def _capture(self: object, output_image: bytes, reference_images: list[bytes], prompt: str):
        seen.append(prompt)
        return fixture

    monkeypatch.setattr(qa_module.GeminiQaProvider, "_call_api", _capture)

    storage_path = await _presign_and_upload_operation(client, api_client_key, "BACKGROUND_REMOVAL")
    await client.post(
        "/api/v2/background/remove",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "qa-prompt-1"},
        json={"storage_path": storage_path},
    )

    assert seen == [qa_module.SUBJECT_PRESERVATION_JUDGE_PROMPT]
    assert qa_module.PIECE_IDENTITY_JUDGE_PROMPT not in seen
