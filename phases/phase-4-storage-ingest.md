# Phase 4 — Storage & Ingest Pipeline

## Reality check before writing this

`POST /uploads/presign` (Phase 1, path fixed in Phase 2 Step 1) already
generates real signed upload URLs against the live Supabase `jewelry-inputs`
bucket, embedding `client_id`:
`pending/{client_id}/{group_id}/{angle}/input_*.jpg`. This is done and is
not rebuilt here.

`job_service.create_job_for_request` (Phase 2) creates an `Asset` row
(`kind=INPUT`) for every uploaded angle at `/generate` time, but before this
phase it only set `bucket`/`storage_path`/`mime_type` (a hardcoded
`"image/jpeg"` regardless of what was actually uploaded) — `width_px`,
`height_px`, `bytes`, `checksum_sha256`, and `expires_at` were always `NULL`,
even though every one of those columns has existed on `assets` since
`docs/schema.md`'s original schema. Two concrete, verified gaps followed
from this:

1. **Retention couldn't work.** `docs/business-rules.md` §11 gives `INPUT`
   assets a 90-day retention window, but nothing ever set `expires_at`, so
   there was no deadline to enforce and no mechanism that enforced one.
2. **Nothing validated that an uploaded "photo" was a real image.**
   `_validate_request`'s only check on a `storage_path` was
   `storage_service.exists` — a directory listing. A client could `PUT`
   arbitrary garbage bytes to the signed upload URL and `/generate` would
   happily create a job from it; the failure would only surface later,
   opaquely, inside the Gemini call in Phase 6. Verified before writing this
   phase by reading `tests/integration/test_generate_real.py`'s existing
   fixture, which uploaded literal `b"test-bytes"` and the request still
   returned `202`.

This phase closes both gaps. It does not touch `POST /uploads/presign`
itself, `/retry`, or `GET /status`'s response shape — those are out of
scope per the task brief. It also does not decide the `OUTPUT` retention
policy (`phases/phase-roadmap.md` open decision #5) — that's a business
decision pending the client, not something this phase can resolve
unilaterally. Instead this phase builds the retention *mechanism* generically
for any `AssetKind`, driven by a single `RETENTION_DAYS` table
(`app/services/retention_policy.py`) that already carries `INPUT: 90`,
`MATTE: 30` (vestigial — no code path produces `MATTE` assets, see
`docs/decisions/0001-drop-local-matting.md`), and `OUTPUT: None`
(indefinite). When decision #5 lands, only that one dict changes.

**Error taxonomy decision:** a corrupt/undecodable/unsupported-format
upload maps to the existing `VALIDATION_ERROR` code (422), not a new code.
`docs/api-routes.md`'s error-code list is meant to be stable
(`docs/conventions.md`: "Changing a code is a breaking API change"), and
`VALIDATION_ERROR` already exists for exactly this shape of problem
("well-formed request, semantically invalid content"). `ASSET_NOT_FOUND`
was considered and rejected — the object *does* exist, it just isn't a
valid image, which is a different failure a client needs to distinguish
(re-upload real bytes, not re-check the path). `docs/business-rules.md` §4
already has an `INVALID_INPUT` **failure class** for exactly this case, but
that classification governs sub-job failures inside generation, not
`/generate`'s synchronous request validation — no code changes attach that
class here, it stays scoped to Phase 6/8 as originally specified.

---

## Step 1 — Retention policy and schema

### What to do

`app/services/retention_policy.py` (new): `RETENTION_DAYS: dict[AssetKind,
int | None]` and `compute_expires_at(kind, now=None) -> datetime | None`,
implementing `docs/business-rules.md` §11 exactly. Single source of truth —
nothing else hardcodes a day count.

Migration `0004_asset_purged_at.py` (chained off `0003`; authored as `0005`
in the parallel worktree this phase was built in, renumbered to `0004` at
merge time once it turned out the sibling Phase 3 work needed no migration
at all): adds `assets.purged_at TIMESTAMPTZ NULL`. Needed because "storage
lifecycle removes bytes" (`docs/business-rules.md` §11) has to know which
expired rows it has already swept, or a beat task run every few hours would
re-attempt (harmless but wasteful, and untestable-as-"done") a storage
delete for the same asset forever. The row itself is still never deleted —
`purged_at` records when its bytes were removed, nothing else.

### Checkpoint 1

- [x] `alembic upgrade head` applies `0004` cleanly on top of `0003`;
      `downgrade()` to `0003` reverses it — both verified by
      `tests/integration/test_migrations.py::test_0004_adds_and_removes_assets_purged_at`
- [x] `retention_policy.compute_expires_at(AssetKind.INPUT)` returns
      `now + 90 days`; `AssetKind.OUTPUT` returns `None`
- [x] `docs/schema.md`'s `assets` table documents `purged_at`

---

## Step 2 — Structural image validation

### What to do

`app/services/image_validation.py` (new): `inspect_and_validate(bucket,
storage_path) -> ImageMetadata` (`width_px`, `height_px`, `bytes`,
`checksum_sha256`, `mime_type`). Downloads the object via
`storage_service.download_to_temp` (already existed, unused until now),
rejects with `InvalidImageError` (`VALIDATION_ERROR`, 422) if: the object is
empty, Pillow can't decode it (`UnidentifiedImageError`/`OSError`), or its
format isn't one of `JPEG`/`PNG`/`WEBP`. This is deliberately *not*
perceptual or content-aware — no local ML, per
`docs/decisions/0001-drop-local-matting.md` and the task's explicit
constraint. It answers "is this a real image file," nothing about what's
depicted.

`app/services/job_service.py`'s `_validate_request` calls this for every
`uploaded` angle (after the existing exists/ownership checks) and now
returns `dict[Angle, ImageMetadata]` instead of `None`, so
`create_job_for_request` doesn't re-download the object when building the
`Asset` row. `assets_repo.create_asset` gained
`width_px`/`height_px`/`bytes_`/`checksum_sha256` parameters; the
`uploaded`-angle branch of `create_job_for_request` now passes the real
extracted values plus `mime_type` from the metadata (not a hardcoded
`"image/jpeg"`) and `expires_at=retention_policy.compute_expires_at(AssetKind.INPUT)`.

### Checkpoint 2

- [x] A garbage-bytes upload (`test-bytes`-style, not a real image) makes
      `/generate` return `422 VALIDATION_ERROR` and creates **no** `Job` row
      — `test_ingest_pipeline.py::test_corrupt_image_rejected_with_validation_error`
- [x] A zero-byte upload is rejected the same way —
      `test_empty_upload_rejected_with_validation_error`
- [x] A structurally valid but unsupported format (BMP) is rejected the
      same way — `test_unsupported_format_rejected_with_validation_error`
- [x] A real PNG upload produces an `Asset` row with the actual decoded
      `width_px`/`height_px`, `bytes == len(uploaded bytes)`, a 64-hex-char
      `checksum_sha256`, and the correct `mime_type` (not hardcoded
      `image/jpeg`) — `test_valid_jpeg_extracts_real_metadata` (despite the
      name, exercises PNG deliberately, to prove `mime_type` isn't
      hardcoded) and the extended assertions in
      `test_generate_real.py::test_happy_path_creates_job_sub_jobs_asset_and_event`
- [x] `expires_at` on a fresh `INPUT` asset is `created_at + 89..90 days`
      (same test)
- [x] `tests/integration/test_generate_real.py`'s existing fixtures were
      updated from placeholder `b"test-bytes"` to a real generated JPEG —
      the pre-Phase-4 fixture would now correctly fail its own request

---

## Step 3 — Retention/expiry lifecycle

### What to do

`app/services/retention_service.py` (new): `expire_assets(session, *,
now=None, limit=500) -> int`. Queries `assets_repo.get_expired_unpurged`
(new repo function: `expires_at IS NOT NULL AND expires_at <= now AND
purged_at IS NULL`), calls `storage_service.delete` (new — wraps Supabase
`storage.from_(bucket).remove([path])`) for each, marks `purged_at`, commits.
Never touches any other column and never issues a `DELETE` against the
`assets` table — Hard Rule 10.

`app/workers/retention.py` (new): Celery task `retention.expire_assets`,
thin — owns only the async session lifecycle
(`app.db.session.async_session_factory`), delegates all logic to the
service. Kept split so the service can be exercised directly against
testcontainers Postgres in tests without a Celery/session-factory
dependency (`docs/conventions.md` layering: workers call services, workers
don't query directly).

`app/workers/celery_app.py`: added `"app.workers.retention"` to `include`,
`"retention.*"` to `task_routes` (routes to the existing `io` queue — no new
queue), and one new `beat_schedule` entry, `asset-retention`, on a new
`settings.RETENTION_SWEEP_CRON` (default `0 3 * * *`, daily at 03:00 UTC).
All additive — the existing `config-sync` entry and Phase 3's sibling work
in this same file are untouched.

Chose a Celery beat task over alternatives (Postgres-native `pg_cron`, a
Supabase Storage bucket lifecycle rule) because: the app already has a beat
scheduler wired for exactly this shape of periodic job
(`config.sync`/`CONFIG_SYNC_CRON`), it keeps the business logic in Python
next to the rest of the codebase instead of split across a second system,
and it's the only option that can update `assets.purged_at` after a delete
— a bucket-native TTL rule would remove bytes with no way to record that a
particular row was affected.

### Checkpoint 3

- [x] An `INPUT` asset with `expires_at` in the past and `purged_at NULL`:
      after `retention_service.expire_assets` runs, the Supabase Storage
      object is gone (`storage_service.exists` returns `False`) and
      `purged_at` is set — `test_retention_worker_purges_bytes_but_keeps_row`
- [x] The `assets` row still exists after the sweep — same test, explicit
      re-`SELECT` by `id`
- [x] An asset with `expires_at IS NULL` (indefinite — e.g. an `OUTPUT`
      asset today) is never selected by the sweep, regardless of age —
      `test_expire_assets_ignores_indefinite_retention`
- [x] `retention.expire_assets` is registered in `celery_app.beat_schedule`
      and routed to the `io` queue, without altering the existing
      `config-sync` entry (visual diff of `celery_app.py`, not a runnable
      assertion — Celery beat scheduling itself isn't exercised end-to-end
      in this phase; see self-audit)

---

## Step 4 — Self-audit

Re-read every checkpoint above and verify by running the real test suite —
testcontainers Postgres, live Supabase Storage for every upload/download/
delete, no mocks — before calling this phase done. Sync `docs/schema.md`
(`purged_at`), `CLAUDE.md`, and `phases/phase-roadmap.md` (this phase's row,
plus open decision #5's status) to match what was actually built. See the
bottom of this file for the honest results, including anything **not**
fully verifiable in this environment.

---

## Note for Phase 5/6

Phase 6 (Gemini Generation Worker) can now trust that any `Asset` row with
`kind=INPUT` it reads for a sub-job is a real, decodable image with correct
metadata — no corrupt-bytes failure mode reaches the provider call anymore.
It should **not** re-validate the image; that would duplicate this phase's
work. It should, however, still handle `expires_at` on retry (already
implemented in Phase 2's `check_retry_preconditions` — unaffected by this
phase, since retention only removes bytes, and `check_retry_preconditions`
already treats an expired-but-present `expires_at` as unretryable
regardless of whether the bytes are physically gone yet).

`OUTPUT` assets Phase 6/7 create should call
`retention_policy.compute_expires_at(AssetKind.OUTPUT)` (currently always
`None`) rather than leaving `expires_at` unset by hand, so the day decision
#5 resolves, every `OUTPUT` asset created from that point picks it up
without another migration or code change to the creation path itself.
