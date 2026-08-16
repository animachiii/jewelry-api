"""Phase 19 Steps 3-4 — POST /api/v2/recolor, operation-aware presign for
RECOLOR (mask_upload), mask contract validation, and the real dispatch /
worker / status / retry machinery, including the generate-then-composite
correctness this operation introduces.

Same stack as tests/integration/test_api_match.py: testcontainers Postgres,
real local Redis, real Supabase Storage (never mocked — this project's own
convention, see docs/ai-integration.md), fixture-driven Gemini
(tests/conftest.py's autouse `_fake_gemini_success_by_default`). Under
`task_always_eager` (also autouse), `POST /api/v2/recolor` dispatches
`recolor.process` inline during the request — a RECOLOR job created here
runs to a real terminal status before the 202 response's own DB reads
happen.
"""

import io
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from app.services import storage_service
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()
_GEMINI_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"

_SOURCE_SIZE = (40, 40)
_PALETTE_CODE = "EMERALD_GREEN"


def _load_gemini_fixture(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((_GEMINI_FIXTURES / name).read_text())
    return result


def _recolor_payload() -> dict[str, Any]:
    """CATEGORY_PAYLOAD doesn't seed operations.RECOLOR/global.palette by
    default — only migration 0016 does, against the real DB. Build a
    payload with RECOLOR enabled and a palette on top of it, same technique
    test_api_match.py's own _match_payload uses.
    """
    payload = dict(CATEGORY_PAYLOAD)
    payload["global"] = dict(payload["global"])
    payload["global"]["operations"] = {
        **payload["global"]["operations"],
        "RECOLOR": {
            "enabled": True,
            "prompt": (
                "Recolor only the gemstone inside the region marked in solid magenta to "
                "{palette_prompt}. Do not alter any metal, prong, setting, or any part of "
                "the image outside the marked region."
            ),
            "unit_cost_usd": 0.02,
        },
    }
    payload["global"]["palette"] = [
        {
            "code": _PALETTE_CODE,
            "label": "Emerald",
            "prompt_phrase": "a deep emerald green emerald",
            "is_active": True,
        },
        {
            "code": "INACTIVE_COLOR",
            "label": "Inactive",
            "prompt_phrase": "an inactive colour",
            "is_active": False,
        },
    ]
    return payload


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
        source_hash="recolor-test-hash",
        payload=_recolor_payload(),
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "recolor-test-client")
    await db_session.commit()
    return raw


def _real_jpeg_bytes(
    size: tuple[int, int] = _SOURCE_SIZE, color: tuple[int, int, int] = (10, 10, 200)
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def _mask_png_bytes(
    size: tuple[int, int] = _SOURCE_SIZE,
    box: tuple[int, int, int, int] = (15, 15, 25, 25),
    mode: str = "L",
    values: tuple[int, int] = (0, 255),
) -> bytes:
    """A mask with a white box in `box` (left, top, right, bottom) on a
    black background — 10x10 = 100 white px of 1600 total = 6.25% coverage,
    comfortably inside the default 0.5%-60% bounds.
    """
    img = Image.new(mode, size, color=values[0])
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            img.putpixel((x, y), values[1])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _presign_recolor(client: AsyncClient, key: str) -> dict[str, Any]:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"operation": "RECOLOR"},
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["angles"] == []
    assert body["operation_upload"] is not None
    assert body["mask_upload"] is not None
    return body


async def _presign_and_upload_recolor(
    client: AsyncClient,
    key: str,
    source_bytes: bytes | None = None,
    mask_bytes: bytes | None = None,
) -> tuple[str, str]:
    body = await _presign_recolor(client, key)
    source_upload = body["operation_upload"]
    mask_upload = body["mask_upload"]

    put_source = httpx.put(
        source_upload["upload_url"],
        content=source_bytes or _real_jpeg_bytes(),
        headers={"Content-Type": "image/jpeg"},
    )
    assert put_source.status_code == 200
    put_mask = httpx.put(
        mask_upload["upload_url"],
        content=mask_bytes or _mask_png_bytes(),
        headers={"Content-Type": "image/png"},
    )
    assert put_mask.status_code == 200
    return str(source_upload["storage_path"]), str(mask_upload["storage_path"])


# --- presign -------------------------------------------------------------


async def test_presign_operation_mode_accepts_recolor_and_returns_mask_upload(
    client: AsyncClient, api_client_key: str
) -> None:
    body = await _presign_recolor(client, api_client_key)
    assert body["operation_upload"]["operation"] == "RECOLOR"
    assert body["mask_upload"]["storage_path"] is not None
    assert body["background_upload"] is None


# --- POST /api/v2/recolor -------------------------------------------------


async def test_recolor_creates_job_and_sub_job(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    storage_path, mask_storage_path = await _presign_and_upload_recolor(client, api_client_key)

    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-1"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
            "sku_reference": "SKU-RECOLOR-1",
        },
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["variants"] == []
    assert len(body["angles"]) == 1
    assert body["angles"][0]["angle"] is None

    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(body["job_id"])))
    ).scalar_one()
    assert job.operation == Operation.RECOLOR
    assert job.category_code is None
    assert job.requested_angles == 1
    assert job.sku_reference == "SKU-RECOLOR-1"
    # Under task_always_eager, the job runs to a real terminal status
    # before this request even returns.
    assert job.status == JobStatus.COMPLETED
    assert job.succeeded_angles == 1

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 1
    sub_job = sub_jobs[0]
    assert sub_job.angle is None
    assert sub_job.variant_index is None
    assert sub_job.palette_code == _PALETTE_CODE
    assert sub_job.mask_asset_id is not None
    assert sub_job.input_asset_id is not None
    # No QA gate — straight to COMPLETED, see docs/business-rules.md §15.
    assert sub_job.status == SubJobStatus.COMPLETED
    assert sub_job.qa_status == QAStatus.NOT_APPLICABLE
    assert sub_job.output_asset_id is not None
    assert sub_job.source_type == SourceType.UPLOADED

    mask_asset = (
        await db_session.execute(select(Asset).where(Asset.id == sub_job.mask_asset_id))
    ).scalar_one()
    assert mask_asset.kind == AssetKind.MASK

    output_asset = (
        await db_session.execute(select(Asset).where(Asset.id == sub_job.output_asset_id))
    ).scalar_one()
    assert output_asset.kind == AssetKind.OUTPUT
    assert output_asset.mime_type == "image/png"

    cost_events = (
        (await db_session.execute(select(CostEvent).where(CostEvent.job_id == job.id)))
        .scalars()
        .all()
    )
    assert len(cost_events) == 1
    assert cost_events[0].operation == "recolor"


async def test_recolor_disabled_operation_422(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = _recolor_payload()
    payload["global"]["operations"]["RECOLOR"]["enabled"] = False
    cv = ConfigVersion(
        version_number=1,
        source_hash="recolor-disabled-hash",
        payload=payload,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    _, key = await _make_client(db_session, "recolor-disabled-client")
    await db_session.commit()

    storage_path, mask_storage_path = await _presign_and_upload_recolor(client, key)
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": key, "Idempotency-Key": "recolor-disabled-1"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "OPERATION_DISABLED"


async def test_recolor_unknown_palette_returns_palette_not_found(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, mask_storage_path = await _presign_and_upload_recolor(client, api_client_key)
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-badpalette"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": "NOT_A_REAL_COLOR",
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "PALETTE_NOT_FOUND"


async def test_recolor_inactive_palette_returns_palette_inactive(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, mask_storage_path = await _presign_and_upload_recolor(client, api_client_key)
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-inactivepalette"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": "INACTIVE_COLOR",
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "PALETTE_INACTIVE"


# --- mask contract, one test per rule -------------------------------------


async def test_recolor_mask_wrong_format_returns_specific_violation(
    client: AsyncClient, api_client_key: str
) -> None:
    body = await _presign_recolor(client, api_client_key)
    source_upload = body["operation_upload"]
    mask_upload = body["mask_upload"]
    httpx.put(source_upload["upload_url"], content=_real_jpeg_bytes())
    # A JPEG where a PNG mask is expected.
    httpx.put(mask_upload["upload_url"], content=_real_jpeg_bytes())

    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-mask-format"},
        json={
            "storage_path": source_upload["storage_path"],
            "mask_storage_path": mask_upload["storage_path"],
            "palette_code": _PALETTE_CODE,
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "PNG" in resp.json()["error"]["message"]


async def test_recolor_mask_wrong_mode_returns_specific_violation(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, mask_storage_path = await _presign_and_upload_recolor(
        client, api_client_key, mask_bytes=_mask_png_bytes(mode="RGB", values=(0, 0))
    )
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-mask-mode"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "grayscale" in resp.json()["error"]["message"]


async def test_recolor_mask_dimension_mismatch_names_both_dimensions(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, mask_storage_path = await _presign_and_upload_recolor(
        client, api_client_key, mask_bytes=_mask_png_bytes(size=(20, 20), box=(5, 5, 10, 10))
    )
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-mask-dims"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["mask_width_px"] == 20
    assert error["details"]["source_width_px"] == _SOURCE_SIZE[0]


async def test_recolor_mask_non_binary_names_intermediate_value_count(
    client: AsyncClient, api_client_key: str
) -> None:
    img = Image.new("L", _SOURCE_SIZE, color=0)
    for y in range(15, 25):
        for x in range(15, 25):
            img.putpixel((x, y), 255)
    # Antialias one edge pixel to an intermediate value.
    img.putpixel((15, 15), 128)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    storage_path, mask_storage_path = await _presign_and_upload_recolor(
        client, api_client_key, mask_bytes=buf.getvalue()
    )
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-mask-binary"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["intermediate_value_count"] == 1


async def test_recolor_mask_below_min_coverage_422(
    client: AsyncClient, api_client_key: str
) -> None:
    # 1x1 white px of 1600 = 0.0625%, below the 0.5% floor.
    storage_path, mask_storage_path = await _presign_and_upload_recolor(
        client, api_client_key, mask_bytes=_mask_png_bytes(box=(0, 0, 1, 1))
    )
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-mask-mincov"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "minimum is 0.5%" in error["message"]


async def test_recolor_mask_above_max_coverage_422(
    client: AsyncClient, api_client_key: str
) -> None:
    # Whole image white = 100% coverage, above the 60% ceiling.
    storage_path, mask_storage_path = await _presign_and_upload_recolor(
        client, api_client_key, mask_bytes=_mask_png_bytes(box=(0, 0, *_SOURCE_SIZE))
    )
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-mask-maxcov"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert "maximum is 60.0%" in error["message"]


# --- idempotency -----------------------------------------------------------


async def test_recolor_replay_returns_original_job_id(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, mask_storage_path = await _presign_and_upload_recolor(client, api_client_key)
    body_json = {
        "storage_path": storage_path,
        "mask_storage_path": mask_storage_path,
        "palette_code": _PALETTE_CODE,
    }
    first = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-replay"},
        json=body_json,
    )
    second = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-replay"},
        json=body_json,
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]


async def test_recolor_same_key_different_payload_409(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, mask_storage_path = await _presign_and_upload_recolor(client, api_client_key)
    await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-conflict"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-conflict"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
            "sku_reference": "different-payload",
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


# --- status ------------------------------------------------------------


async def test_status_for_recolor_job_returns_results(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path, mask_storage_path = await _presign_and_upload_recolor(client, api_client_key)
    create_resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-status"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.get(f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "RECOLOR"
    assert body["angles"] == []
    assert body["variants"] == []
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["status"] == "COMPLETED"
    assert result["image_url"] is not None


# --- retry ---------------------------------------------------------------


async def test_retry_job_dispatches_recolor_process_for_failed_recolor_job(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.providers.gemini import GeminiAPIError, GeminiProvider

    def _boom(self: object, *a: object, **k: object) -> None:
        raise GeminiAPIError(500, "simulated 5xx")

    monkeypatch.setattr(GeminiProvider, "_call_api", _boom)

    storage_path, mask_storage_path = await _presign_and_upload_recolor(client, api_client_key)
    create_resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-fail"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    job_id = create_resp.json()["job_id"]
    job = (await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
    assert job.status == JobStatus.FAILED

    # The in-process attempt loop (recolor_service.MAX_ATTEMPTS) already
    # exhausted attempt_count to the ceiling by the time the sub-job lands
    # on FAILED — the same documented, project-wide "already at the
    # client-retry ceiling" gap Phase 8 found for angle generation
    # (job_service.MAX_RETRY_ATTEMPTS shares the same value/column). This
    # test's actual target is the retry route's dispatch-task selection for
    # Operation.RECOLOR, not attempt-count budgeting, so reset it directly.
    sub_job = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().one()
    )
    sub_job.attempt_count = 0
    await db_session.commit()

    # monkeypatch.undo() would restore GeminiProvider._call_api to its real,
    # unpatched form (a live call with no GEMINI_API_KEY), not back to the
    # autouse success fixture's patch — that fixture uses its own separate
    # monkeypatch instance. Re-apply the same fixture content explicitly.
    monkeypatch.setattr(
        GeminiProvider, "_call_api", lambda self, *a, **k: _load_gemini_fixture("success.json")
    )

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-retry-1"},
    )
    assert resp.status_code == 202, resp.text

    await db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED


# --- off-mask pixel identity (Checkpoint 4's central test) ---------------


async def test_recolor_output_is_byte_identical_to_source_outside_mask(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """The single most important test in this phase — see
    phases/phase-19-recolor.md Step 4/Checkpoint 4. Proves
    generate-then-composite actually works: everywhere the mask was 0, the
    stored OUTPUT asset's pixels must be identical to the original source's
    pixels, regardless of what the (fixture-driven) provider returned.
    """
    # PNG, not the usual JPEG helper — JPEG is lossy even for a flat color
    # (chroma subsampling can shift a channel by ~1), which would make this
    # test's own encode/decode round trip a false positive/negative signal
    # unrelated to compositing correctness. PNG is lossless, so the decoded
    # source pixel is guaranteed to equal source_color exactly.
    source_color = (10, 10, 200)
    source_buf = io.BytesIO()
    Image.new("RGB", _SOURCE_SIZE, color=source_color).save(source_buf, format="PNG")
    source_bytes = source_buf.getvalue()
    # A small mask box, well away from the corners so a MASK_FEATHER_PX=3
    # blur around its edge can't reach the corner pixels checked below.
    mask_bytes = _mask_png_bytes(box=(15, 15, 25, 25))

    storage_path, mask_storage_path = await _presign_and_upload_recolor(
        client, api_client_key, source_bytes=source_bytes, mask_bytes=mask_bytes
    )
    resp = await client.post(
        "/api/v2/recolor",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "recolor-pixel-identity"},
        json={
            "storage_path": storage_path,
            "mask_storage_path": mask_storage_path,
            "palette_code": _PALETTE_CODE,
        },
    )
    assert resp.status_code == 202, resp.text
    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(resp.json()["job_id"])))
    ).scalar_one()
    assert job.status == JobStatus.COMPLETED

    sub_job = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().one()
    )
    output_asset = (
        await db_session.execute(select(Asset).where(Asset.id == sub_job.output_asset_id))
    ).scalar_one()
    output_bytes = storage_service.download_bytes(output_asset.bucket, output_asset.storage_path)
    output_image = Image.open(io.BytesIO(output_bytes)).convert("RGB")

    # Corners are far (>10px) from the mask box at (15,15)-(25,25) — well
    # outside even a generously feathered edge.
    for corner in [(1, 1), (38, 1), (1, 38), (38, 38)]:
        assert output_image.getpixel(corner) == source_color, (
            f"pixel {corner} outside the mask changed: "
            f"expected {source_color}, got {output_image.getpixel(corner)}"
        )
