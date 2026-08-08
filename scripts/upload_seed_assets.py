"""Uploads a tiny placeholder JPEG to Supabase Storage for every seeded OUTPUT
asset that backs a COMPLETED sub-job, and every INPUT asset that backs a
FAILED sub-job. Idempotent — skips paths that already exist.

Seed data (scripts/seed_dev.py) only writes `assets` rows; it never uploads
bytes. Phase 1's mock server needs `image_url` to be a real signed URL that
returns real image bytes on GET (see phases/phase-1-api-contract.md
Checkpoint 3) — Supabase's sign endpoint 404s for a path with no object, so
this is required, not cosmetic. `upload_placeholder_bytes` is also called
directly by tests/integration/test_mock_fixtures.py against
testcontainers-seeded (ephemeral) job rows.

The FAILED/INPUT half was added in Phase 8: a real retry
(app/services/retry_service.py) dispatches a real
generation.transform_photo, which downloads the sub-job's input asset from
Storage — a seeded FAILED sub-job's fabricated storage_path 404s the same
way an un-backfilled COMPLETED output did in Phase 1, just discovered later
because nothing read it until retry became real.

python scripts/upload_seed_assets.py
"""

import asyncio
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Asset, SubJob
from app.db.models.enums import AssetKind, SubJobStatus
from app.db.session import async_session_factory
from app.services import storage_service

# 1x1 white pixel JPEG.
_PLACEHOLDER_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300030202020202030202"
    "02030303030406040404040408060605070907080808070808090a0c0a09090b0906"
    "07080b0d0b0b0c0c0c0c0d0c0c0c0e0e10100e0c1013100e0c0c0f130f10101111120"
    "d0c1414141310100c090c101414151513101010ffc9000b08000100010100021100f"
    "fcc0006001005ffda0008010100003f00d2cf20ffd9"
)


async def upload_placeholder_bytes(session: AsyncSession) -> tuple[int, int]:
    """Backfills bytes for every COMPLETED sub-job's output asset, and every
    FAILED sub-job's input asset, visible in `session`. Returns (uploaded,
    skipped_existing).
    """
    output_result = await session.execute(
        select(Asset)
        .join(SubJob, SubJob.output_asset_id == Asset.id)
        .where(SubJob.status == SubJobStatus.COMPLETED, Asset.kind == AssetKind.OUTPUT)
    )
    input_result = await session.execute(
        select(Asset)
        .join(SubJob, SubJob.input_asset_id == Asset.id)
        .where(SubJob.status == SubJobStatus.FAILED, Asset.kind == AssetKind.INPUT)
    )
    assets = list(output_result.scalars().all()) + list(input_result.scalars().all())

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(_PLACEHOLDER_JPEG)
        tmp_path = Path(tmp.name)

    uploaded, skipped = 0, 0
    for asset in assets:
        if storage_service.exists(asset.bucket, asset.storage_path):
            skipped += 1
            continue
        storage_service.upload_from_temp(asset.bucket, asset.storage_path, tmp_path, "image/jpeg")
        uploaded += 1

    return uploaded, skipped


async def main() -> None:
    async with async_session_factory() as session:
        uploaded, skipped = await upload_placeholder_bytes(session)
    print(f"Done. uploaded={uploaded} skipped(existing)={skipped}")


if __name__ == "__main__":
    asyncio.run(main())
