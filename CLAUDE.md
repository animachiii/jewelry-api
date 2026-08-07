# Project Constitution — AI Jewelry Generation API (v2)

## Project Overview

A headless, asynchronous REST API that turns client-supplied jewelry photographs into
catalog-ready product imagery. A single request submits up to four camera angles
(FRONT, SIDE, DIAGONAL, TOP) for one jewelry category; the backend mattes each subject
locally, sends it to Gemini for background synthesis, relighting, and shadow generation,
and returns per-angle image URLs. Jobs succeed partially by design — three good angles
and one failure is a normal, fully-supported outcome.

There is **no UI in this repository.** The only consumer is the client's Flutter ERP,
which POSTs a job and polls for status.

**Actors:**

| Actor | Can do |
| :--- | :--- |
| Flutter ERP (API client) | Fetch config, request presigned uploads, submit jobs, poll status, retry a single failed angle |
| Ops / prompt author | Edit prompts and reference images in Google Sheets; trigger a config sync |
| Ops / engineer | Read job history, cost reports, QA review queue |

## Tech Stack

FastAPI + Pydantic v2 · Celery 5.4 + Redis · Supabase (Postgres 15 + Storage buckets) ·
Gemini Image API · Docker Compose · Sentry + structlog

## Folder Structure

```
app/
  main.py                 FastAPI app factory, middleware, router registration
  config.py               Pydantic Settings — all env vars, single source
  api/v2/                 Route handlers only. No business logic.
    config.py  generate.py  status.py  retry.py  uploads.py  health.py
    jobs.py  qa.py         Ops routes (Phase 1 Step 2 — not in the original folder
                            sketch; api-routes.md always specified them, they just
                            hadn't been split into their own modules yet)
    schemas/               Pydantic request/response models, one file per route family
  core/
    auth.py                X-API-Key -> Argon2 verify -> ApiClient, scope enforcement
    errors.py              Exception classes + handlers, error envelope, ErrorCode enum
    logging.py             structlog JSON setup, request_id/job_id contextvars
    middleware.py          ULID request-ID middleware (Phase 1 Step 1 — not in the
                            original sketch, request-ID handling needed its own module)
    ratelimit.py           Redis token bucket
    idempotency.py         Idempotency-Key storage, replay, and conflict detection
  db/
    session.py             SQLAlchemy async engine + session factory (+ get_db FastAPI dep)
    models/                ORM models, one file per table group
    repositories/          All queries live here. Routes never query directly.
  services/
    config_service.py      GET /config response assembly from the active config_versions row
    job_service.py          Real POST /generate: validation (docs/business-rules.md §1),
                            job/sub-job/asset creation, idempotency with payload_hash
                            (Phase 2). Also still holds retry-precondition checks for
                            the MOCK_MODE retry endpoint (real retry execution is Phase 8).
    status_rollup.py        compute_parent_status — pure function, docs/business-rules.md §3
                            (Phase 2 — not in the original sketch)
    status_service.py      GET /status response assembly, retryable/signed-URL logic
                            (Phase 1 Step 3 — not in the original sketch)
    storage_service.py     Supabase Storage upload/download/signed URLs
    cost_service.py        Cost event recording
  workers/
    celery_app.py          Celery config, queue routing, beat schedule
    health.py              ping_io verification task (added Step 4, not in original plan)
    generation.py          IO queue: Gemini calls (real-photo and synthetic — no matting step, see docs/decisions/0001-drop-local-matting.md)
    qa.py                  IO queue: perceptual similarity gate (synthetic angles only)
    orchestration.py       Group fan-out, chord callback, parent rollup
  providers/
    base.py                GenerationProvider ABC
    gemini.py               Gemini implementation
migrations/                Alembic
scripts/
  seed_dev.py              Idempotent dev seed data — all 8 job-state scenarios
  export_openapi.py        Writes docs/openapi.json from the live FastAPI app
  upload_seed_assets.py    Backfills real placeholder bytes for seeded COMPLETED
                            output assets (Supabase's sign endpoint 404s otherwise)
tests/
  unit/  integration/  fixtures/
docs/                      Reference files imported below, plus:
  openapi.json              Committed OpenAPI 3.1 spec, diff-checked in CI
  integration-guide.md      Flutter ERP integration guide (Phase 1 deliverable)
phases/                    Phase specification files
```

## Key Architectural Decisions

- **Postgres is the system of record. Redis is not.** Redis holds the Celery broker,
  the config cache, rate-limit buckets, and idempotency keys. Every one of those is
  reconstructible from Postgres or Sheets. Nothing is lost if Redis is flushed.
- **No local ML models.** Background removal, relighting, and shadow generation all
  happen in a single Gemini call — real-photo angles use the same shape as synthetic
  ones. Dropped local BiRefNet matting on 2026-08-07; see
  `docs/decisions/0001-drop-local-matting.md` for the tradeoff this accepted. Celery
  runs a single `io` queue as a result — the old `gpu`/`io` split existed only to
  isolate VRAM-bound matting, and there is no VRAM-bound work left.
- **Partial success is a first-class terminal state,** not an error path. The parent job
  rolls up from sub-job states; a mixed result returns completed URLs plus retry flags.
- **Failures are classified before they are handled.** Transient classes get bounded
  backoff inside the sub-task; deterministic classes fail immediately. "Fail-fast" is a
  policy about deterministic errors, not about network blips.
- **Every job records the config version and model version that produced it.** Without
  this, a bad prompt edit or a silent upstream model change is undiagnosable.
- **Google Sheets is an authoring surface, not a database.** Every sync is snapshotted
  into a versioned, immutable Postgres row. Jobs reference the snapshot, never live Sheets.
- **Synthetic angles are flagged and gated.** An angle generated without a source
  photograph is marked `synthetic` and must pass the QA similarity gate before it is
  returned as completed. Generative models invent jewelry detail; this is the only
  mechanism that catches it.
- **The Gemini client sits behind a `GenerationProvider` interface.** We are not building
  a second provider now, but no task body may import the Gemini SDK directly.

## Hard Rules — Never Break These

1. **Never store job state only in Redis.** Any state the client can observe must be in
   Postgres first, cached second.
2. **Never call a floating Gemini model alias.** Always a pinned version string from
   config, always recorded on the sub-job.
3. **Never write business logic in a route handler.** Routes validate, delegate to a
   service, and serialize. Nothing else.
4. **Never query the database from a route or a task directly.** Go through
   `db/repositories/`.
5. **Never call the live Gemini or Sheets API in tests.** Use recorded fixtures.
6. **Never return a synthetic angle as `COMPLETED` without a QA score.**
7. **Never accept a `/generate` request without checking the `Idempotency-Key`.**
   Duplicate submissions bill the client twice.
8. **Never fail a job because Google Sheets was unreachable.** Fall back to the last
   active `config_versions` row in Postgres.
9. **Never log raw API keys, Supabase service keys, or full signed URLs.**
10. **Never delete an asset row.** Soft-expire via `expires_at`; storage lifecycle
    handles the bytes.
11. **Never mutate a `config_versions` row.** New sync means new version.

## Reference Documentation

- See @docs/schema.md for the full data model — every table, column, type, and relationship.
- See @docs/api-routes.md for every endpoint, method, auth requirement, and payload shape.
- See @docs/business-rules.md for the angle matrix, state machine, partial-success
  computation, retry policy, and cost rules.
- See @docs/ai-integration.md for every AI call site: trigger, model, input, output,
  failure modes.
- See @docs/conventions.md for naming, error handling, logging, and migration rules.
- See @phases/phase-roadmap.md for build order and current phase status.

## Current Status

Phase 0 — Foundation & Environment is **complete** (see prior note on
Steps 1-4/6/7 verified live, Step 5 moot).

Phase 1 — API Contract & Mock Server is **complete pending sign-off**. All
four steps built and verified against the real Supabase project (not a
local stub):

- Error envelope, `ErrorCode` enum (every code in `docs/api-routes.md`),
  exception handlers, ULID request-ID middleware — all test-covered.
- Real `X-API-Key` -> Argon2 -> `ApiClient` auth with `client`/`ops` scope
  enforcement on every route in `docs/api-routes.md`. OpenAPI 3.1 spec
  generated, validated, and committed at `docs/openapi.json`.
- `GET /config`, `GET /status/{job_id}`, `POST /uploads/presign` are real
  reads/writes against Postgres and Supabase Storage — not fixtures. Only
  `POST /generate` and the retry endpoint are behind `MOCK_MODE` (true
  locally, must be false in production), standing in for Phase 2/8 logic
  against real seeded rows.
- All 8 seeded job-state scenarios verified reachable with real signed URLs
  that return real, downloadable image bytes (`scripts/upload_seed_assets.py`
  backfills placeholder bytes, since Supabase's sign endpoint 404s on a path
  with no object — this was a real gap, not a hypothetical one).
- `docs/integration-guide.md` written for the Flutter team, including a
  fresh handoff API key and the seeded `job_id` table.

**One known, accepted spec deviation:** `phases/phase-1-api-contract.md`
assumed an expired signed URL returns `403`. Supabase actually returns `400`
for an expired/invalid signing token — this is Supabase's behavior, not
something this codebase controls, and is documented in
`docs/integration-guide.md` §7.

**Open item — not yet done:** no standing deployment exists for the Flutter
team to reach over the network; the mock server has only been run and
verified locally against the live Supabase project. Client/Flutter sign-off
itself (the actual walkthrough and written confirmation) has not happened —
that requires a human session with the client and Flutter lead, not
something this session can complete unattended. See
`phases/phase-1-api-contract.md` Checkpoint 4 for the exact remaining items.

`app/workers/health.py` was added during Phase 0 Step 4 for the
`health.ping_io` verification task — it isn't in the folder structure below
because Step 4 asked for this without specifying a file and it doesn't
belong in `generation.py` or `qa.py`.

Phase 2 — Data Model & Job State Machine is **complete**, verified live
against the real Supabase project:

- `POST /generate` is real: validates category/angle/synthetic against the
  client's pinned active config version, verifies uploaded `storage_path`s
  exist and belong to the client, creates `Job` + `SubJob` (one per angle,
  correct `status`/`source_type`) + `Asset` (uploaded angles) + a
  `JOB_CREATED` `JobEvent` in one transaction. It no longer checks
  `MOCK_MODE` at all — only `/retry` still does (real retry execution is
  Phase 8).
- Idempotency is durable: `jobs.payload_hash` (migration `0003`) makes the
  same-key/different-payload `409` survive past Redis's 24h TTL; a
  concurrent-insert race on the same new key is caught and resolved as a
  replay.
- `POST /uploads/presign` paths now embed `client_id`
  (`pending/{client_id}/{group_id}/{angle}/...`) so `/generate` can verify
  asset ownership without a database row existing yet — a gap found and
  closed while starting this phase, documented in
  `phases/phase-2-data-model.md`.
- `compute_parent_status` (`app/services/status_rollup.py`) implements
  `docs/business-rules.md` §3 exactly, tested against every row of that
  table — ready for Phase 7/8/9 to call, though nothing calls it yet since
  no sub-job executes in this phase (jobs sit at `PENDING` until a worker
  exists).
- A job created here does not progress on its own — Celery/Gemini
  execution is Phase 6/7's job, not this one's.

Phase 3 — Config Service is **complete**, verified against real local Redis and
testcontainers Postgres: `GET /config` now reads through the Redis `config:active`
cache (15 min TTL, `app/services/config_service.py`) with Postgres as the fallback
on a cache miss or a Redis error, never failing the request while either is up —
`docs/business-rules.md` §9's fallback order. `POST /internal/config/sync`
(`app/services/config_sync_service.py`) is real: it fetches Google Sheets through a
new `app/providers/sheets.py` seam, normalizes rows into the `config_versions.payload`
shape, SHA-256 hashes the normalized payload, and only writes+activates a new
immutable row when the hash changed, invalidating the cache on activation. A
validation failure records a `FAILED` row without activating it; a Sheets outage
(the real state of every environment right now — no Sheets project exists, see
roadmap open decision #2) writes nothing and falls back to the currently active
version instead of failing. A Celery beat task `config.sync` (`app/workers/config.py`)
now backs the `beat_schedule` entry that previously pointed at a nonexistent task.
No migration was needed — `config_versions` already had every column this phase
uses. See `phases/phase-3-config-service.md` for the full self-audit, including
what's explicitly unverified (the real Sheets column layout, and a live beat tick
against standing worker processes).

Phase 4 — Storage & Ingest Pipeline is **complete**, verified live against
the real Supabase project (Storage never mocked): `/generate` now
downloads and structurally validates every uploaded image (decodable,
supported format, non-empty — no local ML, see
`docs/decisions/0001-drop-local-matting.md`) via
`app/services/image_validation.py`, rejecting bad uploads with the existing
`VALIDATION_ERROR` code rather than a new one; `Asset` rows for uploaded
angles now get real `width_px`/`height_px`/`bytes`/`checksum_sha256`/
`mime_type` and a `retention_policy`-computed `expires_at` (`INPUT`: 90
days) instead of `NULL`s and a hardcoded MIME type. A new Celery beat task,
`retention.expire_assets` (`app/workers/retention.py` +
`app/services/retention_service.py`), removes Storage bytes for any expired
asset of any kind and stamps the new `assets.purged_at` column (migration
`0004`) — the row is never deleted. `OUTPUT` retention stays `NULL`
(indefinite) pending the client policy decision (`phases/phase-roadmap.md`
open decision #5); the mechanism is generic across `AssetKind` so that
decision only ever needs to change one dict. See
`phases/phase-4-storage-ingest.md` for the full self-audit.
