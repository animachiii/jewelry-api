"""Idempotent dev seed data. Safe to re-run — see Phase 0 Step 6 checkpoint.

python scripts/seed_dev.py
"""

import asyncio
import hashlib
import secrets
import sys
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiClient, Asset, ConfigVersion, CostEvent, Job, JobEvent, SubJob
from app.db.models.enums import (
    AssetKind,
    FailureClass,
    JobStatus,
    QAStatus,
    SourceType,
    SubJobStatus,
    SyncStatus,
)
from app.db.session import async_session_factory

hasher = PasswordHasher()


def _generate_key() -> tuple[str, str]:
    """Returns (key_prefix, raw_key). Only the Argon2 hash is ever stored.

    key_prefix is docs/schema.md's "first 8 chars of the key, for lookup and
    logs" — the raw key must be fully random from the start, not prefixed
    with a human-readable label. A label like "client_" eats into that fixed
    8-char window and collapses same-scope keys down to ~1 char of entropy,
    causing key_prefix collisions.
    """
    raw = secrets.token_urlsafe(32)
    return raw[:8], raw


async def seed_api_clients(session: AsyncSession) -> dict[str, ApiClient]:
    specs = [
        ("Flutter ERP — dev", "client", True),
        ("Ops console — dev", "ops", True),
        ("Revoked client — dev", "client", False),
    ]
    clients: dict[str, ApiClient] = {}
    for name, scope, is_active in specs:
        existing = (
            await session.execute(select(ApiClient).where(ApiClient.name == name))
        ).scalar_one_or_none()
        if existing is not None:
            clients[name] = existing
            continue
        key_prefix, raw_key = _generate_key()
        client = ApiClient(
            name=name,
            key_prefix=key_prefix,
            key_hash=hasher.hash(raw_key),
            scope=scope,
            is_active=is_active,
            revoked_at=None if is_active else datetime.now(UTC),
        )
        session.add(client)
        await session.flush()
        clients[name] = client
        print(f"  {name}: raw key = {raw_key}  (shown once — only the hash is stored)")
    return clients


CATEGORY_PAYLOAD = {
    "categories": [
        {
            "code": "RING",
            "name": "Rings",
            "is_active": True,
            "angles": {
                "FRONT": {
                    "enabled": True,
                    "synthetic_allowed": False,
                    "prompt": "Studio product photo of a ring, front view.",
                    "reference_image_urls": ["https://example.com/ring-front-ref.jpg"],
                },
                "SIDE": {
                    "enabled": True,
                    "synthetic_allowed": False,
                    "prompt": "Studio product photo of a ring, side view.",
                    "reference_image_urls": [],
                },
                "DIAGONAL": {
                    "enabled": True,
                    "synthetic_allowed": True,
                    "prompt": "Studio product photo of a ring, three-quarter view.",
                    "reference_image_urls": ["https://example.com/ring-diag-ref.jpg"],
                },
                "TOP": {
                    "enabled": False,
                    "synthetic_allowed": False,
                    "prompt": None,
                    "reference_image_urls": [],
                },
            },
        },
        {
            "code": "NECKLACE",
            "name": "Necklaces",
            "is_active": True,
            "angles": {
                "FRONT": {
                    "enabled": True,
                    "synthetic_allowed": False,
                    "prompt": "Studio product photo of a necklace, front view.",
                    "reference_image_urls": [],
                },
                "SIDE": {
                    "enabled": False,
                    "synthetic_allowed": False,
                    "prompt": None,
                    "reference_image_urls": [],
                },
                "DIAGONAL": {
                    "enabled": False,
                    "synthetic_allowed": False,
                    "prompt": None,
                    "reference_image_urls": [],
                },
                "TOP": {
                    "enabled": True,
                    "synthetic_allowed": False,
                    "prompt": "Studio product photo of a necklace, flat lay top view.",
                    "reference_image_urls": [],
                },
            },
        },
    ],
    "global": {
        "model_version": "gemini-2.5-flash-image-preview",
        "qa_similarity_threshold": 0.82,
        "default_negative_prompt": "blurry, distorted, extra gemstones, wrong prong count",
        # Placeholder — Gemini image-generation pricing, to be confirmed against
        # real billing before launch. See docs/schema.md and phases/phase-6-generation-worker.md.
        "unit_cost_usd": 0.02,
        # Placeholder, same status as unit_cost_usd above — see migration
        # 0007 and docs/decisions/0002-background-removal-approach.md.
        # A production DB gets these via migration 0007, not this script;
        # seeded here too so a fresh dev/test DB has real, usable
        # background_presets data for Phase 15 Steps 4/5.
        "operations": {
            "BACKGROUND_REMOVAL": {
                "enabled": True,
                "prompt": (
                    "Replace the background with a clean, seamless white studio "
                    "backdrop. Keep the product subject — its proportions, "
                    "materials, textures, and every detail — exactly unchanged."
                ),
                "unit_cost_usd": 0.02,
            },
            "BACKGROUND_REPLACEMENT": {
                "enabled": True,
                "unit_cost_usd": 0.02,
            },
        },
        "background_presets": [
            {
                "code": "STUDIO_WHITE",
                "name": "Studio White",
                "prompt": (
                    "Place the product on a clean, seamless white studio backdrop "
                    "with soft, even lighting and a subtle natural drop shadow."
                ),
                "reference_image_urls": [],
                "is_active": True,
            }
        ],
        # Placeholder, same status as qa_similarity_threshold — see
        # migration 0010. Deliberately higher: "same object, new
        # background" should score closer to 1.0 than a synthetic angle's
        # "novel view of the same object."
        "background_qa_similarity_threshold": 0.92,
    },
}


async def seed_config_versions(session: AsyncSession) -> ConfigVersion:
    active = (
        await session.execute(select(ConfigVersion).where(ConfigVersion.is_active))
    ).scalar_one_or_none()
    if active is not None:
        return active

    old_payload = {
        **CATEGORY_PAYLOAD,
        "global": {**CATEGORY_PAYLOAD["global"], "model_version": "gemini-2.0-flash-preview"},
    }
    old = ConfigVersion(
        version_number=1,
        source_hash=hashlib.sha256(b"seed-v1").hexdigest(),
        payload=old_payload,
        sync_status=SyncStatus.SUCCESS,
        is_active=False,
    )
    session.add(old)

    active_version = ConfigVersion(
        version_number=2,
        source_hash=hashlib.sha256(b"seed-v2").hexdigest(),
        payload=CATEGORY_PAYLOAD,
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    session.add(active_version)
    await session.flush()
    return active_version


async def _make_job(
    session: AsyncSession,
    client: ApiClient,
    config_version: ConfigVersion,
    idempotency_key: str,
    requested_angles: int,
    status: JobStatus,
    succeeded: int,
    failed: int,
) -> Job:
    job = Job(
        client_id=client.id,
        idempotency_key=idempotency_key,
        payload_hash=hashlib.sha256(idempotency_key.encode()).hexdigest(),
        category_code="RING",
        config_version_id=config_version.id,
        status=status,
        requested_angles=requested_angles,
        succeeded_angles=succeeded,
        failed_angles=failed,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC) if status != JobStatus.PROCESSING else None,
    )
    session.add(job)
    await session.flush()
    session.add(
        JobEvent(
            job_id=job.id, event_type="JOB_CREATED", to_status=status.value, detail={"seed": True}
        )
    )
    return job


async def _add_sub_job(
    session: AsyncSession,
    job: Job,
    angle: str,
    status: SubJobStatus,
    source_type: SourceType,
    *,
    failure_class: FailureClass | None = None,
    qa_status: QAStatus = QAStatus.NOT_APPLICABLE,
    qa_score: float | None = None,
    input_expired: bool = False,
) -> SubJob:
    input_asset = None
    if source_type == SourceType.UPLOADED and status != SubJobStatus.SKIPPED:
        input_asset = Asset(
            job_id=job.id,
            kind=AssetKind.INPUT,
            bucket="jewelry-inputs",
            storage_path=f"{job.id}/{angle}/input_{secrets.token_hex(4)}.jpg",
            mime_type="image/jpeg",
            expires_at=datetime.now(UTC) - timedelta(days=1)
            if input_expired
            else datetime.now(UTC) + timedelta(days=90),
        )
        session.add(input_asset)
        await session.flush()

    sub_job = SubJob(
        job_id=job.id,
        angle=angle,
        status=status,
        source_type=source_type,
        input_asset_id=input_asset.id if input_asset else None,
        failure_class=failure_class,
        qa_status=qa_status,
        qa_score=qa_score,
        model_version=CATEGORY_PAYLOAD["global"]["model_version"]
        if status == SubJobStatus.COMPLETED
        else None,
        completed_at=datetime.now(UTC)
        if status in (SubJobStatus.COMPLETED, SubJobStatus.FAILED, SubJobStatus.REJECTED)
        else None,
    )
    session.add(sub_job)
    await session.flush()

    if status == SubJobStatus.COMPLETED:
        output_asset = Asset(
            job_id=job.id,
            sub_job_id=sub_job.id,
            kind=AssetKind.OUTPUT,
            bucket="jewelry-outputs",
            storage_path=f"{job.id}/{angle}/output_{secrets.token_hex(4)}.jpg",
            mime_type="image/jpeg",
        )
        session.add(output_asset)
        await session.flush()
        sub_job.output_asset_id = output_asset.id

    if status in (SubJobStatus.COMPLETED, SubJobStatus.FAILED, SubJobStatus.REJECTED):
        session.add(
            CostEvent(
                job_id=job.id,
                sub_job_id=sub_job.id,
                provider="gemini",
                operation="image_generation",
                model_version=CATEGORY_PAYLOAD["global"]["model_version"],
                unit_cost_usd=CATEGORY_PAYLOAD["global"]["unit_cost_usd"],
                total_cost_usd=CATEGORY_PAYLOAD["global"]["unit_cost_usd"],
            )
        )

    return sub_job


async def seed_jobs(
    session: AsyncSession, client: ApiClient, config_version: ConfigVersion
) -> None:
    existing = (
        (await session.execute(select(Job).where(Job.idempotency_key.like("seed-%"))))
        .scalars()
        .first()
    )
    if existing is not None:
        return

    angles = ["FRONT", "SIDE", "DIAGONAL", "TOP"]

    # 1. 4/4 succeeded -> COMPLETED
    j = await _make_job(
        session, client, config_version, "seed-completed", 4, JobStatus.COMPLETED, 4, 0
    )
    for a in angles:
        await _add_sub_job(session, j, a, SubJobStatus.COMPLETED, SourceType.UPLOADED)

    # 2. 3 succeeded, 1 FAILED (retryable) -> PARTIAL_SUCCESS
    j = await _make_job(
        session,
        client,
        config_version,
        "seed-partial-retryable",
        4,
        JobStatus.PARTIAL_SUCCESS,
        3,
        1,
    )
    for a in angles[:3]:
        await _add_sub_job(session, j, a, SubJobStatus.COMPLETED, SourceType.UPLOADED)
    await _add_sub_job(
        session,
        j,
        angles[3],
        SubJobStatus.FAILED,
        SourceType.UPLOADED,
        failure_class=FailureClass.TRANSIENT_PROVIDER,
    )

    # 3. 3 succeeded, 1 REJECTED (safety refusal) -> PARTIAL_SUCCESS, not retryable
    j = await _make_job(
        session, client, config_version, "seed-partial-rejected", 4, JobStatus.PARTIAL_SUCCESS, 3, 1
    )
    for a in angles[:3]:
        await _add_sub_job(session, j, a, SubJobStatus.COMPLETED, SourceType.UPLOADED)
    await _add_sub_job(
        session,
        j,
        angles[3],
        SubJobStatus.REJECTED,
        SourceType.UPLOADED,
        failure_class=FailureClass.SAFETY_REFUSAL,
    )

    # 4. 0 succeeded, 4 failed -> FAILED
    j = await _make_job(
        session, client, config_version, "seed-all-failed", 4, JobStatus.FAILED, 0, 4
    )
    for a in angles:
        await _add_sub_job(
            session,
            j,
            a,
            SubJobStatus.FAILED,
            SourceType.UPLOADED,
            failure_class=FailureClass.TRANSIENT_NETWORK,
        )

    # 5. 1 angle only, failed -> FAILED, not PARTIAL_SUCCESS
    j = await _make_job(
        session, client, config_version, "seed-single-angle-failed", 1, JobStatus.FAILED, 0, 1
    )
    await _add_sub_job(
        session,
        j,
        "FRONT",
        SubJobStatus.FAILED,
        SourceType.UPLOADED,
        failure_class=FailureClass.INVALID_INPUT,
    )
    await _add_sub_job(session, j, "SIDE", SubJobStatus.SKIPPED, SourceType.UPLOADED)
    await _add_sub_job(session, j, "DIAGONAL", SubJobStatus.SKIPPED, SourceType.UPLOADED)
    await _add_sub_job(session, j, "TOP", SubJobStatus.SKIPPED, SourceType.UPLOADED)

    # 6. 2 requested, 2 skipped, both succeeded -> COMPLETED, SKIPPED excluded from math
    j = await _make_job(
        session, client, config_version, "seed-two-skipped", 2, JobStatus.COMPLETED, 2, 0
    )
    await _add_sub_job(session, j, "FRONT", SubJobStatus.COMPLETED, SourceType.UPLOADED)
    await _add_sub_job(session, j, "SIDE", SubJobStatus.COMPLETED, SourceType.UPLOADED)
    await _add_sub_job(session, j, "DIAGONAL", SubJobStatus.SKIPPED, SourceType.UPLOADED)
    await _add_sub_job(session, j, "TOP", SubJobStatus.SKIPPED, SourceType.UPLOADED)

    # 7. Synthetic angle in QA_REVIEW -> parent stays PROCESSING
    j = await _make_job(
        session, client, config_version, "seed-qa-review", 4, JobStatus.PROCESSING, 3, 0
    )
    for a in ["FRONT", "SIDE", "TOP"]:
        await _add_sub_job(session, j, a, SubJobStatus.COMPLETED, SourceType.UPLOADED)
    await _add_sub_job(
        session,
        j,
        "DIAGONAL",
        SubJobStatus.QA_REVIEW,
        SourceType.SYNTHETIC,
        qa_status=QAStatus.FLAGGED,
        qa_score=0.71,
    )

    # 8. In-flight: 2 done, 2 GENERATING -> PROCESSING
    j = await _make_job(
        session, client, config_version, "seed-in-flight", 4, JobStatus.PROCESSING, 2, 0
    )
    await _add_sub_job(session, j, "FRONT", SubJobStatus.COMPLETED, SourceType.UPLOADED)
    await _add_sub_job(session, j, "SIDE", SubJobStatus.COMPLETED, SourceType.UPLOADED)
    await _add_sub_job(session, j, "DIAGONAL", SubJobStatus.GENERATING, SourceType.UPLOADED)
    await _add_sub_job(
        session, j, "TOP", SubJobStatus.GENERATING, SourceType.UPLOADED, input_expired=True
    )


async def main() -> None:
    async with async_session_factory() as session:
        print("Seeding api_clients...")
        clients = await seed_api_clients(session)
        print("Seeding config_versions...")
        config_version = await seed_config_versions(session)
        print("Seeding jobs/sub_jobs/assets/cost_events (8 scenarios)...")
        await seed_jobs(session, clients["Flutter ERP — dev"], config_version)
        await session.commit()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
