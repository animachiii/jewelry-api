# Phase 3 — Config Service

## Reality check before writing this

Phase 2 left `GET /api/v2/config` doing a real Postgres read of the active
`config_versions` row, with no Redis involved at all — `app/services/config_service.py`
said so explicitly in its own docstring ("Redis caching... lands in Phase 3").
`POST /api/v2/internal/config/sync` was a stub: `app/api/v2/config.py` raised
`NotImplementedError` unconditionally, already gated behind `require_ops_scope`.
`app/config.py`'s `Settings` already has `CONFIG_SYNC_CRON`, `GOOGLE_SERVICE_ACCOUNT_JSON`,
and `CONFIG_SHEET_ID` — all three empty in every `.env` in this project, because no
real Google Sheets project has been created yet (this is expected, not a gap this
phase can close — see roadmap open decision #2: "The 7 exact category codes and
per-category angle enablement" is still open). `app/workers/celery_app.py` already
had a `beat_schedule["config-sync"]` entry pointing at a task named `config.sync`
that did not exist anywhere in the codebase — the beat schedule was wired to a
task Phase 0 never created.

The `config_versions` table (`docs/schema.md`) already has every column this phase
needs — `source_hash`, `sync_status`, `error_message`, `is_active`, `activated_at`,
the partial unique index on `is_active`. **No migration is needed for this phase.**

No real Google Sheet exists (confirmed, not assumed — `GOOGLE_SERVICE_ACCOUNT_JSON`
and `CONFIG_SHEET_ID` are empty in this worktree's `.env`, copied from the shared
checkout). This phase builds the real Sheets client code behind a seam
(`app/providers/sheets.fetch_sheet_rows`) so `app/services/config_sync_service.py`
never imports the Sheets SDK directly and tests inject a fixture — the same pattern
`docs/ai-integration.md` already establishes for Gemini. Because there's no real
sheet, the actual column layout used by `normalize_sheet_rows` is an **assumed
convention** (one row per category/angle pair on an `Angles` tab, key/value pairs
on a `Global` tab — documented in `app/providers/sheets.py`'s module docstring),
not something confirmed against the client's real spreadsheet. The sync pipeline
itself (fetch → normalize → hash → version → activate → invalidate) is fully real
and fully tested; only the exact spreadsheet column mapping is provisional.

Redis is real and reachable locally (`redis://localhost:6379/0`) — used for real in
this phase's service code, not mocked. `fakeredis` remains available for isolated
unit-style tests but the phase's checkpoint tests exercise real Redis so cache
hit/miss/invalidation/TTL behavior is actually proven, not assumed from a fake.

---

## Step 1 — Redis dependency + config cache

### What to do

Add `app/core/redis_client.py`: a module-level singleton `redis.asyncio.Redis`
(same pattern as `app/core/idempotency.py`'s `_client()`) plus a FastAPI
dependency `get_redis()` so routes can `Depends(get_redis)` without services
importing FastAPI (`docs/conventions.md` layering rule).

Rewrite `app/services/config_service.py` to add the `config:active` cache
(`docs/schema.md`: 15 min TTL) in front of the existing Postgres read:
`get_config_response(session, redis)` — cache hit returns immediately; cache
miss (including a Redis error, treated identically) reads the active Postgres
row, builds the response, writes it back to cache, and returns it; no active
Postgres row raises `ConfigUnavailableError` (503). Add `invalidate_cache(redis)`
for the sync endpoint to call after activating a new version. Any `RedisError`
from a cache read or write is caught and logged as a `warning`, never allowed to
fail the request — this is the "Redis cache → active Postgres row → hard
failure only if both are unavailable" order from `docs/business-rules.md` §9,
applied to reads generally, not just Sheets-outage reads.

Wire `app/api/v2/config.py`'s `GET /config` route to call this instead of
reading the repository directly.

### Checkpoint 1

- [x] Cache miss: `GET /config` populates `config:active` in real Redis, with a
      TTL between 0 and 900 seconds inclusive of the upper bound
- [x] Cache hit: a value manually seeded into `config:active` (with a
      deliberately different `config_version` than the active Postgres row) is
      what `GET /config` returns — proves the cache is actually consulted first,
      not merely written to
- [x] `GET /config` response never contains `prompt` or `reference_image_urls`
      keys (unchanged from Phase 1/2, re-verified here since the response now
      flows through a cache round-trip)

---

## Step 2 — Sheets client seam

### What to do

Add `app/providers/sheets.py`: `fetch_sheet_rows() -> SheetRows` (a `NamedTuple`
of `angle_rows` / `global_rows`, raw `list[list[str]]` as the Sheets API would
return them). Raises `SheetsUnavailableError` — not configured, network error,
auth error, or any other failure — always the same exception type, because every
one of those cases must be handled identically by the caller (docs/business-rules.md
§9: a Sheets outage must never fail anything downstream). This is the only file
that imports `googleapiclient`/`google-auth` — consistent with
`docs/conventions.md`'s "the only place that imports a model SDK" rule for
`app/providers/`, applied here to an external-API SDK for the same isolation
reason.

### Checkpoint 2

- [x] `fetch_sheet_rows()` raises `SheetsUnavailableError` when
      `GOOGLE_SERVICE_ACCOUNT_JSON`/`CONFIG_SHEET_ID` are empty (true in every
      environment right now) — verified by calling it directly against this
      worktree's real `.env`, not mocked
- [x] No test in `tests/` imports `googleapiclient` or calls a live Sheets
      endpoint (Hard Rule 5)

---

## Step 3 — Normalization, hashing, and the sync pipeline

### What to do

Add `app/services/config_sync_service.py`:

- `normalize_sheet_rows(rows: SheetRows) -> dict` — pure function, raw rows to
  the `config_versions.payload` shape (`docs/schema.md`). Deterministic
  ordering (categories sorted by code) so a sheet-row reorder with no content
  change hashes identically. Raises `ConfigValidationError` (a `ValueError`
  subclass) on any structurally invalid input: unknown angle name, duplicate
  angle for one category, no rows at all, missing `model_version`, or
  `qa_similarity_threshold` outside `[0, 1]`.
- `compute_source_hash(payload: dict) -> str` — SHA-256 over
  `json.dumps(payload, sort_keys=True)`.
- `sync_config(session, redis, fetch_rows=fetch_sheet_rows) -> ConfigVersion` —
  the orchestration, with `fetch_rows` injectable so tests exercise the full
  pipeline with a fixture instead of the real Sheets client:
  1. `fetch_rows()`. On `SheetsUnavailableError`: log a warning, return the
     current active version unchanged (re-raising only if there is no active
     version to fall back to — both Redis and Postgres unavailable is the only
     hard-failure case per §9). **No new row is written on an outage** — there
     is no payload to record, so nothing is `FAILED` about it; this is
     deliberately different from the validation-failure case below, which does
     write a row.
  2. On successful fetch, `normalize_sheet_rows`. On `ConfigValidationError`:
     write a new `config_versions` row with `sync_status: FAILED` and
     `error_message` set, `is_active` left `false`, commit, and return the
     still-active previous version — `docs/business-rules.md` §9's "a sync
     that fails validation is recorded FAILED and does not become active."
  3. On successful normalization, hash the payload. Unchanged hash vs. the
     active version: no new row, return the active version unchanged
     (idempotent sync, `docs/api-routes.md`).
  4. Changed (or no active version at all): create the new row via
     `app/db/repositories/config_versions.create_version`, activate it via
     `activate_version` (deactivates whatever was active first, inside the same
     transaction, so the partial unique index on `is_active` is never
     transiently violated across two committed statements), commit, invalidate
     the Redis cache, return the new version.

Add repository writes to `app/db/repositories/config_versions.py`:
`get_next_version_number`, `create_version`, `activate_version` — routes and
workers still never build a `select()`/`insert()` directly.

Wire `POST /api/v2/internal/config/sync` in `app/api/v2/config.py` to call
`sync_config`, ops-scope-gated (unchanged from Phase 1), returning `202` with
`{config_version, sync_status, activated}` (new `ConfigSyncResponse` schema).

### Checkpoint 3

- [x] `normalize_sheet_rows` builds the documented payload shape from valid
      rows, including filling any angle missing from the sheet as
      `enabled: false` rather than omitting it
- [x] `normalize_sheet_rows` rejects (via `ConfigValidationError`): unknown
      angle name, duplicate angle for one category, no category rows, missing
      `model_version`, `qa_similarity_threshold` outside `[0, 1]`
- [x] `compute_source_hash` is stable under dict key reordering and under
      sheet-row reordering (same categories, different row order), and changes
      when the payload actually changes
- [x] Unchanged hash: `sync_config` creates no new `config_versions` row,
      returns the existing active version's identity
- [x] Changed hash: `sync_config` creates exactly one new row, activates it,
      and the previously active row is now `is_active = false` — verified by
      querying Postgres directly, not just checking the returned object
- [x] At any point in time, at most one `config_versions` row has `is_active =
      true` — verified by query after two consecutive syncs
- [x] A validation failure writes a `FAILED` row with `error_message` set and
      `is_active = false`, and the previously active row is still the one
      `get_active` returns afterward
- [x] A Sheets outage (real: no Sheets configured in this environment) writes
      **no** new row at all — verified by counting `config_versions` rows
      before and after calling the real `POST /internal/config/sync` endpoint
- [x] A successful new-version sync deletes the `config:active` Redis key
      (verified against real Redis, not fakeredis)
- [x] `POST /internal/config/sync` returns `403` for a `client`-scope key

---

## Step 4 — Celery beat task

### What to do

Add `app/workers/config.py`: a Celery task named `config.sync` (matching the
name already referenced by `celery_app.conf.beat_schedule["config-sync"]` and
`task_routes["config.sync"]`, both of which predate this phase and were dead
references until now) that opens its own session via
`app.db.session.async_session_factory`, gets the shared Redis client via
`app.core.redis_client.get_redis_client()`, and calls `sync_config`. Register
`app.workers.config` in `celery_app.py`'s `include` list.

### Checkpoint 4

- [x] `"config.sync"` is a registered Celery task name after importing
      `app.workers.config`
- [x] `celery_app.conf.task_routes["config.sync"] == {"queue": "io"}`
      (unchanged routing, now backed by a real task)
- [x] `celery_app.conf.beat_schedule["config-sync"]["task"] == "config.sync"`
      and its `schedule` is built from `settings.CONFIG_SYNC_CRON`
- [x] The task function's registered Celery name (`config_worker.sync.name`)
      is `"config.sync"`, confirming the decorator and the beat schedule refer
      to the same task

Not verified: an actual Celery worker process consuming a real beat tick
end-to-end (would require running `celery beat` + `celery worker` as
standing processes against the real broker, which is infrastructure
verification, not phase-scoped code verification — the task's *logic* is
covered by Checkpoint 3's tests via direct calls to `sync_config`, and the
task's *wiring* is covered by Checkpoint 4).

---

## Step 5 — Self-audit

Re-read every checkpoint above. Every one is backed by a real, passing test —
`tests/unit/test_config_sync_service.py` (pure normalization/hashing, 14
tests), `tests/unit/test_config_beat_schedule.py` (Celery wiring, 4 tests),
`tests/integration/test_config_service.py` (12 tests against real
testcontainers Postgres + real local Redis: cache hit/miss/TTL, sync scope
enforcement, Sheets-outage fallback, validation failure, hash-unchanged
idempotency, hash-changed activation/deactivation, single-active-row
invariant, cache invalidation). Full suite: 48 unit + 56 integration tests,
all passing. `ruff format`, `ruff check`, and `mypy --strict app/` all clean.

**Explicitly not verifiable in this session, stated rather than silently
skipped:**

- The real Google Sheets column layout `normalize_sheet_rows` assumes is
  unconfirmed against the client's actual spreadsheet (roadmap open decision
  #2 is still open). The sync *pipeline* is real and tested; the specific
  row→payload mapping is a placeholder convention that will need revisiting
  — documented in `app/providers/sheets.py` and `docs/schema.md` — once a
  real sheet exists. This does not block later phases: Phase 6/7 consume
  `config_versions.payload`, not raw Sheets rows.
- A standing `celery beat` + `celery worker` process actually firing
  `config.sync` on a live 15-minute tick was not run end-to-end (see Step 4
  checkpoint note) — the task registration, routing, and schedule wiring are
  verified; the live scheduler tick is an infrastructure concern for a
  running deployment, not something this session can observe without
  standing up long-lived processes.

Docs synced: `docs/api-routes.md`'s sync section now documents the response
shape and the three distinct outcomes (outage-fallback / validation-failure /
new-version-activated); `docs/schema.md` notes where the Sheets normalization
lives and that the column layout is provisional. `CLAUDE.md` and
`phases/phase-roadmap.md` updated with this phase's status.

---

## Note for Phase 4

Phase 4 (Storage & Ingest Pipeline) does not depend on this phase — the
roadmap already marks them parallel-eligible after Phase 2. Phase 6 (Gemini
Generation Worker) is the first phase that actually reads
`config_versions.payload`'s `prompt`/`reference_image_urls`/`model_version`
fields for real (this phase's `GET /config` response deliberately excludes
them — `docs/api-routes.md`: "Prompts and reference image URLs are not
exposed to the client"). When Phase 6 needs a real Sheets project's category
codes and angle enablement, roadmap open decision #2 will need to be
resolved first, and `app/providers/sheets.py`'s assumed column layout should
be revisited against whatever the client's real sheet looks like — ideally
without needing to change `config_sync_service.py`'s `normalize_sheet_rows`
contract, only the row-parsing details inside it.
