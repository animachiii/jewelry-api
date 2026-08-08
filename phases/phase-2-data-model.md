# Phase 2 — Data Model & Job State Machine

## Reality check before writing this

Phase 1 shipped `POST /generate` and the retry endpoint behind `MOCK_MODE`:
`/generate` hands back an existing job for the client instead of creating
one; `GET /config`, `GET /status/{job_id}`, and `POST /uploads/presign` are
already real reads/writes against Postgres and Supabase Storage. Phase 2
replaces the `/generate` mock with real job/sub-job creation. It does **not**
touch `/retry` (that stays mock — real retry execution needs Phase 8's
orchestration) and does **not** execute any sub-job (no Celery task runs
here — that's Phase 7). A job created in this phase sits at `PENDING` with
its sub-jobs at `PENDING`/`SKIPPED` until a later phase's worker picks it up.

Two gaps found while starting this phase, both closed here rather than
carried forward:

1. **Idempotency conflict detection has no durable payload record.**
   `docs/business-rules.md` §8 requires "same key + different payload → 409."
   Phase 1's mock stored `job_id|payload_hash` in Redis only (24h TTL) — fine
   for a demo, not durable. Real idempotency needs the hash to outlive Redis,
   so this phase adds a `payload_hash` column to `jobs` via migration.
2. **`POST /uploads/presign` doesn't scope its path to a client.** The
   Phase 1 path convention is `pending/{group_id}/{angle}/input_*.jpg` — no
   client identity in it, so `/generate`'s "storage_path belongs to this
   client" check (`docs/api-routes.md` validation rule 4) has nothing to
   check against. Fixed by embedding `client_id` in the presign path:
   `pending/{client_id}/{group_id}/{angle}/input_*.jpg`. This is an internal
   convention the client never parses (they quote the path back verbatim),
   so it's not a contract change.

---

## Step 1 — Schema: payload_hash, presign path fix

### What to do

Migration `0003_add_jobs_payload_hash.py`: add `jobs.payload_hash TEXT NOT
NULL`. Backfill isn't needed (no real jobs exist outside seed/dev data).
Update `docs/schema.md`'s `jobs` table.

Fix `app/api/v2/uploads.py` to build `pending/{client.id}/{group_id}/{angle}/
input_*.jpg`. Update the docstring explaining the convention.

### Checkpoint 1

- [ ] `alembic upgrade head` applies cleanly on a fresh DB and on top of
      0001/0002; `alembic check` (or equivalent drift check) is clean
      afterward
- [ ] `downgrade()` reverses the column add
- [ ] `docs/schema.md` documents `payload_hash`
- [ ] Presigned `storage_path` starts with `pending/{client_id}/`

---

## Step 2 — Repositories: writes

### What to do

Add write functions alongside the existing read-only repositories (routes
still never build a `select()`/`insert()` directly — see
`docs/conventions.md`):

- `app/db/repositories/jobs.py`: `create_job`, `create_sub_job`
- `app/db/repositories/assets.py`: `create_asset`
- `app/db/repositories/job_events.py` (new file): `record_event`

### Checkpoint 2

- [ ] Each write function takes an open `AsyncSession` and the fields it
      needs, adds the row, and does **not** commit (the route/service
      controls the transaction boundary — see docs/conventions.md "Parent
      status recomputation and the sub-job transition that triggered it
      happen in the same transaction")

---

## Step 3 — Parent-status rollup service

### What to do

`app/services/status_rollup.py`: `compute_parent_status(requested,
succeeded, failed) -> JobStatus`, implementing the table in
`docs/business-rules.md` §3 exactly. Pure function, no I/O — every later
phase that transitions a sub-job calls this and persists the result in the
same transaction as the transition.

Phase 2 itself never calls this against non-zero succeeded/failed (nothing
executes sub-jobs yet), but it must be correct and fully tested now since
Phase 7/8/9 depend on it without re-deriving it.

### Checkpoint 3

- [ ] A parametrized test covers every row of the §3 table, including the
      single-angle-failure case (`FAILED`, not `PARTIAL_SUCCESS`) and the
      `SKIPPED`-exclusion case

---

## Step 4 — Real `POST /generate`

### What to do

Replace the `MOCK_MODE` branch in `app/api/v2/generate.py`. New
`app/services/job_service.py` function `create_job_for_request` doing, in
order (`docs/api-routes.md` validation order):

1. Idempotency check — `get_by_idempotency_key`. If found: compare
   `payload_hash`; equal → return the original job's accepted response
   unchanged (still `202`); different → `409 IDEMPOTENCY_KEY_CONFLICT`.
2. Validate `category_code` exists and is active in the client's pinned
   active `config_versions` row → `422 CATEGORY_NOT_FOUND` /
   `CATEGORY_INACTIVE`.
3. Validate every requested (non-skipped) angle is `enabled` for that
   category → `422 ANGLE_NOT_ENABLED`.
4. Validate any `synthetic` angle has `synthetic_allowed: true` → `422
   SYNTHETIC_NOT_ALLOWED`.
5. Validate every `storage_path` exists in `jewelry-inputs` and starts with
   `pending/{client_id}/` → `422 ASSET_NOT_FOUND` / `ASSET_NOT_OWNED`.
6. Validate at least one angle is not skipped → `422 NO_ANGLES_REQUESTED`.
7. Create `Job` (status `PENDING`, `config_version_id` pinned to the active
   version, `payload_hash` set) + one `SubJob` per angle (`SKIPPED` for
   skipped angles, `PENDING` otherwise, `source_type` per mode) + one
   `Asset` (`kind=INPUT`) per uploaded angle, + one `JobEvent`
   (`JOB_CREATED`) — all in one transaction.
8. Handle the race: unique-constraint violation on insert (two concurrent
   requests, same new key) → re-fetch and treat as step 1's replay path.

Real `Idempotency-Key` handling replaces the Redis-only mock: Redis
(`idem:{client_id}:{key}` → `job_id`, 24h TTL per `docs/schema.md`) is a
fast-path cache in front of the Postgres row, which is the permanent source
of truth for the conflict check.

`/retry` is untouched — still `MOCK_MODE`-gated, still Phase 8's job.

### Checkpoint 4

- [ ] Happy path: valid request creates a `Job` + correct `SubJob` rows
      (right `status`/`source_type` per angle mode) + `Asset` rows for
      uploaded angles + one `JOB_CREATED` `JobEvent`, all visible via
      `GET /status` immediately after
- [ ] Idempotent replay (same key, same payload) returns the original
      `job_id`, creates no new row
- [ ] Same key, different payload → `409 IDEMPOTENCY_KEY_CONFLICT`, no new
      row
- [ ] Each of the 6 validation failures is reachable and returns the
      documented code, and **no job row is created** on any of them
- [ ] `storage_path` under another client's `pending/{client_id}/` prefix →
      `422 ASSET_NOT_OWNED`
- [ ] `MOCK_MODE=false` still works for `/generate` now (it's real); `/retry`
      still raises when `MOCK_MODE=false` (unchanged from Phase 1)

---

## Step 5 — Self-audit

Same discipline as Phase 1: re-read every checkpoint above, verify by
running real tests against testcontainers Postgres (+ live Supabase Storage
for the asset-existence check, consistent with how Phase 1 tested), fix
failures before declaring done, sync `docs/schema.md` / `CLAUDE.md` /
`phases/phase-roadmap.md` to match what was actually built.

---

## Note for Phase 3

Phase 3 (Config Service — Sheets sync, Redis cache) can start independently
once this phase is done; it doesn't depend on job creation. Phase 6/7
(generation worker, orchestration) are what actually make a created job
progress past `PENDING` — until then, every job created here will sit
unexecuted, which is correct for this phase's scope.
