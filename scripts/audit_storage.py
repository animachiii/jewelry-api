"""Phase 16 Step 4 — storage audit. Connects to the real Supabase project
(via `storage.objects`, the same catalog table the Storage API itself reads
— querying it directly gives an exact, complete accounting rather than a
sampled one, for buckets where "sample 500" would still miss the shape of
the anomaly) and reports, per bucket: object count, size distribution,
content-type distribution, and how many objects have no corresponding
`assets` row.

Usage:
    python scripts/audit_storage.py
"""

import asyncio

from sqlalchemy import text

from app.db.session import async_session_factory

BUCKETS = ["jewelry-inputs", "jewelry-outputs", "jewellery-gen"]


async def _audit_bucket(session, bucket: str) -> None:
    exists = await session.execute(
        text("select 1 from storage.buckets where name = :b"), {"b": bucket}
    )
    if exists.scalar_one_or_none() is None:
        print(f"\n=== {bucket} === not reachable from this project, skipping")
        return

    stats = (
        (
            await session.execute(
                text("""
                select
                    count(*) as object_count,
                    coalesce(sum((o.metadata->>'size')::bigint), 0) as total_bytes,
                    coalesce(round(avg((o.metadata->>'size')::bigint)), 0) as avg_bytes,
                    coalesce(min((o.metadata->>'size')::bigint), 0) as min_bytes,
                    coalesce(
                        percentile_cont(0.5)
                            within group (order by (o.metadata->>'size')::bigint), 0
                    ) as p50_bytes,
                    coalesce(
                        percentile_cont(0.95)
                            within group (order by (o.metadata->>'size')::bigint), 0
                    ) as p95_bytes,
                    coalesce(max((o.metadata->>'size')::bigint), 0) as max_bytes
                from storage.objects o join storage.buckets b on b.id = o.bucket_id
                where b.name = :bucket
            """),
                {"bucket": bucket},
            )
        )
        .mappings()
        .one()
    )

    content_types = (
        (
            await session.execute(
                text("""
                select o.metadata->>'mimetype' as mime, count(*) as n
                from storage.objects o join storage.buckets b on b.id = o.bucket_id
                where b.name = :bucket
                group by 1 order by n desc
            """),
                {"bucket": bucket},
            )
        )
        .mappings()
        .all()
    )

    # Cross-reference against assets.storage_path directly (bucket +
    # storage_path is that table's own unique key) rather than guessing at
    # path structure — jewelry-inputs and jewelry-outputs use different
    # conventions (docs/schema.md: outputs are `{job_id}/{angle}/...`,
    # but a presigned input keeps its `pending/{client_id}/{group_id}/...`
    # path — it is never renamed after the job is created), so a
    # path-prefix guess is wrong for one of the two buckets. This is exact.
    orphans = (
        await session.execute(
            text("""
                select count(*)
                from storage.objects o join storage.buckets b on b.id = o.bucket_id
                where b.name = :bucket
                  and not exists (
                      select 1 from public.assets a
                      where a.bucket = :bucket and a.storage_path = o.name
                  )
            """),
            {"bucket": bucket},
        )
    ).scalar_one()

    tiny_sample = (
        (
            await session.execute(
                text("""
                select o.name, (o.metadata->>'size')::bigint as size_bytes,
                       o.metadata->>'mimetype' as mime, o.created_at
                from storage.objects o join storage.buckets b on b.id = o.bucket_id
                where b.name = :bucket and (o.metadata->>'size')::bigint < 2048
                order by o.created_at desc limit 5
            """),
                {"bucket": bucket},
            )
        )
        .mappings()
        .all()
    )

    print(f"\n=== {bucket} ===")
    print(f"  objects: {stats['object_count']}")
    print(
        f"  size (bytes): min={stats['min_bytes']} p50={stats['p50_bytes']} "
        f"avg={stats['avg_bytes']} p95={stats['p95_bytes']} max={stats['max_bytes']}"
    )
    print(f"  total: {stats['total_bytes'] / 1_048_576:.2f} MB")
    print(f"  content types: {dict((r['mime'], r['n']) for r in content_types)}")
    if bucket == "jewellery-gen":
        print("  (V1 bucket — not part of this app's assets table, no orphan check)")
    else:
        print(f"  objects with NO matching assets row: {orphans} / {stats['object_count']}")
    print("  sample of objects under 2KB:")
    for row in tiny_sample:
        print(f"    {row['name']}  {row['size_bytes']}B  {row['mime']}  {row['created_at']}")


async def main_async() -> None:
    async with async_session_factory() as session:
        for bucket in BUCKETS:
            await _audit_bucket(session, bucket)

        total = (
            await session.execute(
                text("select coalesce(sum((metadata->>'size')::bigint), 0) from storage.objects")
            )
        ).scalar_one()
        print("\n=== TOTAL across all buckets in this project ===")
        print(f"  {total / 1_048_576:.2f} MB of the Supabase free-tier 500MB ceiling")


if __name__ == "__main__":
    asyncio.run(main_async())
