# Storage Audit — 2026-08-15 (Phase 16 Step 4)

Live audit against the real Supabase project (`rsolykmjupiusdujajgj`), run via
`scripts/audit_storage.py`. Full accounting via `storage.objects` (the same catalog
table the Storage API itself reads), not a sample — exact, not estimated.

## The anomaly and its real cause

`jewelry-outputs` held 39,656 objects averaging ~511 bytes — nowhere near a real
generated image. The leading hypothesis going in (a bug writing an error/placeholder
object on a failed or QA-rejected generation) **was wrong**. The real cause:

**Every integration test in this suite uploads real bytes to this real, shared
Supabase project.** `docs/ai-integration.md` deliberately never mocks Storage — Phase
0 through Phase 15 all verified against it live, on purpose. What none of those phases
addressed: the Postgres row a test creates alongside an upload lives in that test's
ephemeral testcontainers database and is gone at teardown, but the Storage object is
not, unless something removes it. Nothing did.

Cross-referencing `storage.objects` against `assets.storage_path` (bucket + path is
that table's own unique key) confirms it exactly:

| Bucket | Objects | With no matching `assets` row | Total size |
| :--- | ---: | ---: | ---: |
| `jewelry-inputs` | 17,449 | 17,404 (99.7%) | 157.3 MB |
| `jewelry-outputs` | 39,656 | 39,618 (99.9%) | 19.3 MB |

The 38/39,618 legitimate `jewelry-outputs` objects (and the equivalent handful in
`jewelry-inputs`) are 1:1 with a real `assets` row — every application code path that
writes an OUTPUT asset (`generation_service._complete_success`,
`background_service._complete_success`) only ever runs after a real image is in hand,
confirmed by reading both. **This was never a production-code bug.** Object timestamps
cluster in bursts seconds apart (test-run shaped, not traffic-shaped), and the daily
volume tracks development activity, not client usage — 19,745 objects arrived on
2026-08-13 alone, the day of heaviest Phase 15 work.

## `jewellery-gen` — out of scope, not V2's problem

A third bucket, `jewellery-gen` (1,746 objects, 308.0 MB — the largest single
contributor to this project's total), is V1's (`animachiii/jewellery-gen-backend`)
production bucket, not this codebase's. `CLAUDE.md`: "V1 stays untouched." It has
nothing to do with this app's `assets` table and was excluded from the orphan check and
from every cleanup action in this phase for that reason. It is real, load-bearing V1
data as far as this audit can tell — this phase did not investigate it further, since
doing so isn't this app's concern.

**Important caveat for Phase 17 (AWS deployment) and any future capacity planning:**
Supabase's storage quota is project-wide, not per-bucket. `jewellery-gen` alone already
consumes 308 MB of the 500 MB free-tier ceiling regardless of anything V2 does. V2's own
housekeeping (below) cannot fully solve a shared-project capacity problem on its own.

## Fix

1. **Root cause (ongoing):** `tests/conftest.py` gained an autouse
   `_cleanup_storage_uploads` fixture (built on a new `track_storage_uploads` context
   manager) that records every `(bucket, path)` a test uploads and deletes them all at
   teardown. Regression-tested against real Storage in
   `tests/integration/test_storage_cleanup_fixture.py`. The test suite will no longer
   grow this bucket.
2. **Existing pollution (one-time):** deleted live — see "One-time cleanup" below.
3. **A real, related gap found while fixing this:** `_complete_success` in both
   `generation_service.py` and `background_service.py` never passed `expires_at` to
   `create_asset` at all — every OUTPUT asset got `NULL` regardless of
   `RETENTION_DAYS[AssetKind.OUTPUT]`. Setting that value away from indefinite (below)
   would have been silently inert without this fix. Both now call
   `retention_policy.compute_expires_at(AssetKind.OUTPUT)`, regression-tested in
   `tests/integration/test_output_asset_retention.py`.

## `OUTPUT` retention — defaulted, not resolved

Roadmap open decision #5 has sat as "mechanism ready, policy value pending" since
Phase 4. `RETENTION_DAYS[AssetKind.OUTPUT]` (`app/services/retention_policy.py`) is now
**180 days**, not `None` — driven by the real capacity pressure this audit found (484.6
MB of 500 MB, dominated by V1's untouchable bucket), not by the client having actually
answered the question. The client can change this to any value, including back to
indefinite, at any time — it is one dict entry. See `phases/phase-roadmap.md`'s open
decisions table for the corresponding update.

## One-time cleanup — executed and verified live

`scripts/cleanup_orphaned_test_objects.py --apply` ran against live Supabase Storage
(architect confirmed before running — deleting real bytes from production is
irreversible). Deleted exactly the 57,022 objects the orphan query identified
(17,404 from `jewelry-inputs`, 39,618 from `jewelry-outputs`), batched 500 paths per
Storage API call, `jewellery-gen` never touched. Re-queried live afterward to confirm:

| Bucket | Objects before | Objects after | MB after |
| :--- | ---: | ---: | ---: |
| `jewelry-inputs` | 17,449 | 45 | 129.55 |
| `jewelry-outputs` | 39,656 | 38 | 13.81 |
| `jewellery-gen` (untouched) | 1,746 | 1,746 | 308.03 |
| **TOTAL** | — | **1,829** | **451.39** |

`jewelry-outputs`' remaining 38 objects match exactly the 38 real, job-backed output
assets found earlier in this audit — that bucket is now clean, no discrepancy either
direction.

`jewelry-inputs` retained more bytes than the pre-cleanup orphan/legit split suggested
at a glance: its ~45 legitimate input photos average **~2.9 MB each** (real product
photography), so despite being under 1% of *object count* pre-cleanup, they always
dominated *byte count* — the 157.26 MB pre-cleanup total was mostly these large real
files plus a long tail of kilobyte-sized test placeholders, not mostly the placeholders
by size. (An earlier version of this report projected the post-cleanup total at
~308 MB, assuming the orphaned objects were most of the bytes as well as most of the
count — that assumption was wrong; corrected here against the actual measured result.)

**One pre-existing, unrelated discrepancy found, not caused by this cleanup:** 26
`assets` rows in `jewelry-inputs` reference storage objects that don't exist — checked
directly, not swept by this phase's cleanup (their `created_at` is 2026-08-07, well
before it ran). Their `storage_path` shape (`{job_id}/{angle}/input_*`) matches
`scripts/seed_dev.py`'s seeded demo jobs; `scripts/upload_seed_assets.py`'s own
docstring only ever promised to backfill **output** assets for seeded `COMPLETED`
rows, never input bytes. A pre-existing dev-fixture gap, not a Phase 16 regression and
not client-facing — flagged here rather than left unexplained, not fixed as out of
this phase's scope.

## Numbers, for the record

- **Total after cleanup: 451.39 MB** of the 500 MB free-tier ceiling (90%) — down from
  484.63 MB before, a real but modest reduction, not the larger one first projected.
- `jewellery-gen` (V1, out of scope, untouched): 308.03 MB — 68% of the current total,
  and the real long-term capacity constraint regardless of anything V2 does.
- V2's own two buckets combined: 143.36 MB. `jewelry-outputs` is now exactly 1:1 with
  real `assets` rows; `jewelry-inputs` has one pre-existing, unrelated 26-row gap (see
  above) neither caused nor fixed by this cleanup.
- The 90%-of-ceiling headroom is thin. If V1's bucket also grows, or real V2 traffic
  scales up, this project will hit the free-tier limit — worth flagging to the
  architect ahead of Phase 17, independent of anything AWS-related, since it's a
  Supabase-side constraint AWS migration doesn't change.
