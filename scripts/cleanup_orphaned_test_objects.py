"""Phase 16 Step 4 — one-time deletion of orphaned test-run objects found by
scripts/audit_storage.py (see docs/storage-audit-2026-08.md). Deletes only
objects in `jewelry-inputs`/`jewelry-outputs` with no matching `assets` row
— never touches `jewellery-gen` (V1's bucket, out of scope; CLAUDE.md: "V1
stays untouched") and never touches an object that has a real `assets` row,
regardless of size or age.

The underlying leak is fixed going forward by tests/conftest.py's
`_cleanup_storage_uploads` fixture — this script only clears what already
accumulated before that fix existed.

Usage:
    python scripts/cleanup_orphaned_test_objects.py           # dry run, reports only
    python scripts/cleanup_orphaned_test_objects.py --apply    # actually deletes
"""

import asyncio
import sys

from sqlalchemy import text

from app.db.session import async_session_factory
from app.services import storage_service

BUCKETS = ["jewelry-inputs", "jewelry-outputs"]
BATCH_SIZE = 500


async def _orphaned_paths(session, bucket: str) -> list[str]:
    result = await session.execute(
        text("""
            select o.name
            from storage.objects o join storage.buckets b on b.id = o.bucket_id
            where b.name = :bucket
              and not exists (
                  select 1 from public.assets a
                  where a.bucket = :bucket and a.storage_path = o.name
              )
        """),
        {"bucket": bucket},
    )
    return [row[0] for row in result.all()]


def _batched(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def main_async() -> None:
    apply = "--apply" in sys.argv

    async with async_session_factory() as session:
        per_bucket: dict[str, list[str]] = {}
        for bucket in BUCKETS:
            paths = await _orphaned_paths(session, bucket)
            per_bucket[bucket] = paths
            print(f"{bucket}: {len(paths)} orphaned objects (no matching assets row)")

    total = sum(len(v) for v in per_bucket.values())
    if not apply:
        print(
            f"\nDry run only — {total} objects would be deleted. Pass --apply to delete for real."
        )
        return

    client = storage_service.get_client()
    deleted = 0
    for bucket, paths in per_bucket.items():
        for batch in _batched(paths, BATCH_SIZE):
            if not batch:
                continue
            client.storage.from_(bucket).remove(batch)
            deleted += len(batch)
            print(f"  deleted {deleted}/{total}...", end="\r")
    print(f"\nDeleted {deleted} orphaned objects.")


if __name__ == "__main__":
    asyncio.run(main_async())
