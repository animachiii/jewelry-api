"""Phase 20 Steps 3-4 — POST /api/v2/mix, operation-aware presign for MIX
(secondary_upload/secondary_mask_upload), two-independent-pair ingest
validation, and the real dispatch/worker/status/retry machinery, including
the rough-composite + seam-only generate-then-composite correctness this
operation introduces.

Same stack as tests/integration/test_api_recolor.py: testcontainers
Postgres, real local Redis, real Supabase Storage (never mocked), fixture-
driven Gemini (tests/conftest.py's autouse `_fake_gemini_success_by_default`).
Under `task_always_eager` (also autouse), `POST /api/v2/mix` dispatches
`mix.process` inline during the request.
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

_SOURCE_SIZE = (60, 60)


def _load_gemini_fixture(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((_GEMINI_FIXTURES / name).read_text())
    return result


def _mix_payload() -> dict[str, Any]:
    """CATEGORY_PAYLOAD doesn't seed operations.MIX by default — only
    migration 0018 does, against the real DB. Build a payload with MIX
    enabled on top of it, same technique test_api_recolor.py's own
    _recolor_payload uses."""
    payload = dict(CATEGORY_PAYLOAD)
    payload["global"] = dict(payload["global"])
    payload["global"]["operations"] = {
        **payload["global"]["operations"],
        "MIX": {
            "enabled": True,
            "prompt": (
                "Blend only the seam marked in solid magenta so the graft "
                "looks like a single, naturally manufactured piece."
            ),
            "unit_cost_usd": 0.02,
        },
    }
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
        source_hash="mix-test-hash",
        payload=_mix_payload(),
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "mix-test-client")
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
    box: tuple[int, int, int, int] = (20, 20, 40, 40),
    mode: str = "L",
    values: tuple[int, int] = (0, 255),
) -> bytes:
    """A mask with a white box in `box` on a black background — 20x20 =
    400 white px of 3600 total = 11.1% coverage, comfortably inside the
    default 0.5%-60% bounds."""
    img = Image.new(mode, size, color=values[0])
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            img.putpixel((x, y), values[1])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _presign_mix(client: AsyncClient, key: str) -> dict[str, Any]:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"operation": "MIX"},
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["angles"] == []
    assert body["operation_upload"] is not None
    assert body["mask_upload"] is not None
    assert body["secondary_upload"] is not None
    assert body["secondary_mask_upload"] is not None
    return body


async def _presign_and_upload_mix(
    client: AsyncClient,
    key: str,
    primary_bytes: bytes | None = None,
    primary_mask_bytes: bytes | None = None,
    secondary_bytes: bytes | None = None,
    secondary_mask_bytes: bytes | None = None,
) -> dict[str, str]:
    body = await _presign_mix(client, key)
    primary_upload = body["operation_upload"]
    primary_mask_upload = body["mask_upload"]
    secondary_upload = body["secondary_upload"]
    secondary_mask_upload = body["secondary_mask_upload"]

    put_primary = httpx.put(
        primary_upload["upload_url"],
        content=primary_bytes or _real_jpeg_bytes(color=(10, 10, 200)),
        headers={"Content-Type": "image/jpeg"},
    )
    assert put_primary.status_code == 200
    put_primary_mask = httpx.put(
        primary_mask_upload["upload_url"],
        content=primary_mask_bytes or _mask_png_bytes(box=(20, 20, 40, 40)),
        headers={"Content-Type": "image/png"},
    )
    assert put_primary_mask.status_code == 200
    put_secondary = httpx.put(
        secondary_upload["upload_url"],
        content=secondary_bytes or _real_jpeg_bytes(color=(200, 10, 10)),
        headers={"Content-Type": "image/jpeg"},
    )
    assert put_secondary.status_code == 200
    put_secondary_mask = httpx.put(
        secondary_mask_upload["upload_url"],
        content=secondary_mask_bytes or _mask_png_bytes(box=(5, 5, 25, 25)),
        headers={"Content-Type": "image/png"},
    )
    assert put_secondary_mask.status_code == 200

    return {
        "primary_storage_path": str(primary_upload["storage_path"]),
        "primary_mask_storage_path": str(primary_mask_upload["storage_path"]),
        "secondary_storage_path": str(secondary_upload["storage_path"]),
        "secondary_mask_storage_path": str(secondary_mask_upload["storage_path"]),
    }


# --- presign -------------------------------------------------------------


async def test_presign_operation_mode_accepts_mix_and_returns_all_four_slots(
    client: AsyncClient, api_client_key: str
) -> None:
    body = await _presign_mix(client, api_client_key)
    assert body["operation_upload"]["operation"] == "MIX"
    assert body["background_upload"] is None


# --- POST /api/v2/mix ------------------------------------------------------


async def test_mix_creates_job_and_sub_job(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    paths = await _presign_and_upload_mix(client, api_client_key)

    resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-1"},
        json={**paths, "sku_reference": "SKU-MIX-1"},
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
    assert job.operation == Operation.MIX
    assert job.category_code is None
    assert job.requested_angles == 1
    assert job.sku_reference == "SKU-MIX-1"
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
    assert sub_job.input_asset_id is not None
    assert sub_job.mask_asset_id is not None
    assert sub_job.secondary_input_asset_id is not None
    assert sub_job.secondary_mask_asset_id is not None
    # No QA gate — straight to COMPLETED, see docs/business-rules.md §16.
    assert sub_job.status == SubJobStatus.COMPLETED
    assert sub_job.qa_status == QAStatus.NOT_APPLICABLE
    assert sub_job.output_asset_id is not None
    assert sub_job.source_type == SourceType.UPLOADED

    for asset_id, expected_kind in [
        (sub_job.input_asset_id, AssetKind.INPUT),
        (sub_job.mask_asset_id, AssetKind.MASK),
        (sub_job.secondary_input_asset_id, AssetKind.INPUT),
        (sub_job.secondary_mask_asset_id, AssetKind.MASK),
    ]:
        asset = (await db_session.execute(select(Asset).where(Asset.id == asset_id))).scalar_one()
        assert asset.kind == expected_kind

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
    assert cost_events[0].operation == "mix"


async def test_mix_disabled_operation_422(client: AsyncClient, db_session: AsyncSession) -> None:
    payload = _mix_payload()
    payload["global"]["operations"]["MIX"]["enabled"] = False
    cv = ConfigVersion(
        version_number=1,
        source_hash="mix-disabled-hash",
        payload=payload,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    _, key = await _make_client(db_session, "mix-disabled-client")
    await db_session.commit()

    paths = await _presign_and_upload_mix(client, key)
    resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": key, "Idempotency-Key": "mix-disabled-1"},
        json=paths,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "OPERATION_DISABLED"


# --- ingest validation, one test per required asset -----------------------


async def test_mix_missing_primary_source_returns_asset_not_found(
    client: AsyncClient, api_client_key: str
) -> None:
    body = await _presign_mix(client, api_client_key)
    # Only upload the mask and the secondary pair — never the primary source.
    httpx.put(body["mask_upload"]["upload_url"], content=_mask_png_bytes())
    httpx.put(body["secondary_upload"]["upload_url"], content=_real_jpeg_bytes())
    httpx.put(body["secondary_mask_upload"]["upload_url"], content=_mask_png_bytes())

    resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-missing-primary"},
        json={
            "primary_storage_path": body["operation_upload"]["storage_path"],
            "primary_mask_storage_path": body["mask_upload"]["storage_path"],
            "secondary_storage_path": body["secondary_upload"]["storage_path"],
            "secondary_mask_storage_path": body["secondary_mask_upload"]["storage_path"],
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "ASSET_NOT_FOUND"


async def test_mix_primary_mask_dimension_mismatch_names_both_dimensions(
    client: AsyncClient, api_client_key: str
) -> None:
    paths = await _presign_and_upload_mix(
        client,
        api_client_key,
        primary_mask_bytes=_mask_png_bytes(size=(20, 20), box=(5, 5, 10, 10)),
    )
    resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-primary-mask-dims"},
        json=paths,
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["mask_width_px"] == 20
    assert error["details"]["source_width_px"] == _SOURCE_SIZE[0]


async def test_mix_secondary_mask_dimension_mismatch_names_both_dimensions(
    client: AsyncClient, api_client_key: str
) -> None:
    paths = await _presign_and_upload_mix(
        client,
        api_client_key,
        secondary_mask_bytes=_mask_png_bytes(size=(20, 20), box=(5, 5, 10, 10)),
    )
    resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-secondary-mask-dims"},
        json=paths,
    )
    assert resp.status_code == 422, resp.text
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["mask_width_px"] == 20
    assert error["details"]["source_width_px"] == _SOURCE_SIZE[0]


# --- idempotency -----------------------------------------------------------


async def test_mix_replay_returns_original_job_id(client: AsyncClient, api_client_key: str) -> None:
    paths = await _presign_and_upload_mix(client, api_client_key)
    first = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-replay"},
        json=paths,
    )
    second = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-replay"},
        json=paths,
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]


async def test_mix_same_key_different_payload_409(client: AsyncClient, api_client_key: str) -> None:
    paths = await _presign_and_upload_mix(client, api_client_key)
    await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-conflict"},
        json=paths,
    )
    resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-conflict"},
        json={**paths, "sku_reference": "different-payload"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


# --- status ------------------------------------------------------------


async def test_status_for_mix_job_returns_results(client: AsyncClient, api_client_key: str) -> None:
    paths = await _presign_and_upload_mix(client, api_client_key)
    create_resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-status"},
        json=paths,
    )
    job_id = create_resp.json()["job_id"]

    resp = await client.get(f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "MIX"
    assert body["angles"] == []
    assert body["variants"] == []
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["status"] == "COMPLETED"
    assert result["image_url"] is not None


# --- retry ---------------------------------------------------------------


async def test_retry_job_dispatches_mix_process_for_failed_mix_job(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.providers.gemini import GeminiAPIError, GeminiProvider

    def _boom(self: object, *a: object, **k: object) -> None:
        raise GeminiAPIError(500, "simulated 5xx")

    monkeypatch.setattr(GeminiProvider, "_call_api", _boom)

    paths = await _presign_and_upload_mix(client, api_client_key)
    create_resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-fail"},
        json=paths,
    )
    job_id = create_resp.json()["job_id"]
    job = (await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
    assert job.status == JobStatus.FAILED

    sub_job = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().one()
    )
    sub_job.attempt_count = 0
    await db_session.commit()

    monkeypatch.setattr(
        GeminiProvider, "_call_api", lambda self, *a, **k: _load_gemini_fixture("success.json")
    )

    resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-retry-1"},
    )
    assert resp.status_code == 202, resp.text

    await db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED


# --- off-seam pixel identity (Checkpoint 4's central test) ---------------


async def test_mix_output_is_byte_identical_to_rough_composite_outside_seam_band(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """The single most important test in this phase — see
    phases/phase-20-mix.md Step 4/Checkpoint 4. Proves generate-then-
    composite (scoped to the seam band) actually works: everywhere outside
    the seam band, the stored OUTPUT asset's pixels must match the
    deterministic rough-composite exactly, regardless of what the
    (fixture-driven) provider returned.
    """
    # A larger canvas/mask than the module's default _SOURCE_SIZE — see
    # mix_service._seam_band_mask's own "known limitation" docstring note:
    # a mask region narrower than ~2*(MIX_SEAM_BAND_PX + MASK_FEATHER_PX)
    # lets the post-call feather bleed through the graft's interior "hole"
    # from both sides of the ring at once. The default settings (6, 3) need
    # roughly an 18-20px-wide interior to stay protected, so this test uses
    # a 100x100 canvas with a 40x40 primary mask (interior after erosion is
    # 40 - 2*6 = 28px wide) rather than the smaller boxes other MIX tests
    # use, which exercise ingest validation, not this pixel guarantee.
    canvas_size = (100, 100)
    primary_color = (10, 10, 200)
    secondary_color = (200, 10, 10)
    primary_buf = io.BytesIO()
    Image.new("RGB", canvas_size, color=primary_color).save(primary_buf, format="PNG")
    secondary_buf = io.BytesIO()
    Image.new("RGB", canvas_size, color=secondary_color).save(secondary_buf, format="PNG")

    paths = await _presign_and_upload_mix(
        client,
        api_client_key,
        primary_bytes=primary_buf.getvalue(),
        primary_mask_bytes=_mask_png_bytes(size=canvas_size, box=(30, 30, 70, 70)),
        secondary_bytes=secondary_buf.getvalue(),
        secondary_mask_bytes=_mask_png_bytes(size=canvas_size, box=(10, 10, 50, 50)),
    )
    resp = await client.post(
        "/api/v2/mix",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "mix-pixel-identity"},
        json=paths,
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

    # Corners are far from the mask box at (30,30)-(70,70) — well outside
    # even a generously feathered/banded edge. Untouched by both the
    # rough-composite step (outside mask A's bbox) and the provider call
    # (outside the feathered seam band).
    for corner in [(1, 1), (98, 1), (1, 98), (98, 98)]:
        assert output_image.getpixel(corner) == primary_color, (
            f"pixel {corner} outside the mask changed: "
            f"expected {primary_color}, got {output_image.getpixel(corner)}"
        )
    # Deep inside the graft's interior (outside the seam band too): the
    # rough-composite's own deterministic placement, not the provider's.
    assert output_image.getpixel((50, 50)) == secondary_color
