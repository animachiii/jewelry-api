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
    ratelimit.py           Redis fixed-window per-client counter, wired into
                            POST /generate for real (Phase 10 — built Phase 0/1,
                            dead code until now)
    idempotency.py         Idempotency-Key storage, replay, and conflict detection
  db/
    session.py             SQLAlchemy async engine + session factory (+ get_db FastAPI dep)
    models/                ORM models, one file per table group
    repositories/          All queries live here. Routes never query directly.
  services/
    config_service.py      GET /config response assembly from the active config_versions row
    job_service.py          Real POST /generate: validation (docs/business-rules.md §1),
                            job/sub-job/asset creation, idempotency with payload_hash
                            (Phase 2). Also holds check_retry_preconditions, the shared
                            §5 precondition checks used by the real retry endpoint
                            (Phase 8 — execution itself is retry_service.py).
    retry_service.py        execute_retry: resets a FAILED sub-job to PENDING, moves a
                            terminal job back to PROCESSING, records RETRY_REQUESTED
                            (Phase 8 — not in the original sketch).
    status_rollup.py        compute_parent_status — pure function, docs/business-rules.md §3
                            (Phase 2 — not in the original sketch)
    status_service.py      GET /status response assembly, retryable/signed-URL logic
                            (Phase 1 Step 3 — not in the original sketch)
    storage_service.py     Supabase Storage upload/download/signed URLs
    cost_service.py        Cost event recording (Phase 6)
    generation_service.py  Real single-sub-job generation call: prompt resolution,
                            Mode A/B input sourcing, rate limiting, cost logging,
                            failure classification (Phase 6 — not in the original
                            sketch; app/workers/generation.py is a thin wrapper)
    rate_limiter.py         Redis fixed-window counter for the Gemini provider,
                            `provider:gemini:tokens:{minute}` (Phase 6 — distinct
                            from app/core/ratelimit.py's per-client API limiting,
                            still unwired, Phase 10)
    orchestration_service.py  dispatch_job — marks a job PROCESSING, returns the
                            sub-job IDs to fan out (Phase 7 — not in the original
                            sketch; app/workers/orchestration.py is a thin wrapper)
    qa_service.py            score_synthetic_angle (automatic scoring, fail-open-to-
                            human on provider error), get/build_review_queue_items,
                            submit_qa_decision (approve/reject) (Phase 9 — not in the
                            original sketch; app/workers/qa.py is a thin wrapper).
                            Reuses generation_service.recompute_parent_status
                            (renamed from _recompute_parent_status, Phase 9) rather
                            than duplicating the rollup-and-persist logic.
  workers/
    celery_app.py          Celery config, queue routing, beat schedule
    health.py              ping_io verification task (added Step 4, not in original plan)
    _async_utils.py         run_async — lets a sync Celery task body call async
                            service code whether or not it's already inside a
                            running event loop (Phase 7 — not in the original
                            sketch; needed once /generate started dispatching
                            work from its own async route handler)
    generation.py          `generation.transform_photo` — session lifecycle only,
                            real logic in services/generation_service.py (Phase 6).
                            Builds its own DB engine per call from
                            settings.DATABASE_URL read live, not the shared
                            app.db.session.async_session_factory (Phase 7 — see
                            phases/phase-7-orchestration.md for why)
    qa.py                  `qa.score_similarity` — session lifecycle only, real logic
                            in services/qa_service.py (Phase 9). Dispatched by
                            generation.py right after a QA_REVIEW-landing
                            transform_photo commits, not from inside the service.
    orchestration.py       `orchestration.fan_out_job` — a loop of independent
                            generation.transform_photo dispatches, not a Celery
                            group/chord (Phase 7 — see phases/phase-7-orchestration.md's
                            reality-check section for why)
  providers/
    base.py                GenerationProvider ABC + GenerationResult (Phase 6)
    gemini.py               GeminiProvider — the only module that imports google.genai,
                            and only inside a deferred _call_api seam (Phase 6)
    qa_base.py              QaProvider ABC + QaResult, mirrors base.py (Phase 9)
    gemini_qa.py             GeminiQaProvider — LLM-judged similarity scoring, same
                            deferred-import isolation as gemini.py (Phase 9)
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

Phase 6 — Gemini Generation Worker is **complete**: `GenerationProvider`
(`app/providers/base.py`) is a real abstraction — `GeminiProvider`
(`app/providers/gemini.py`) is the only module that imports `google.genai`,
and even there the import is deferred inside a `_call_api` seam so unit
tests never touch the network (no real `GEMINI_API_KEY` exists in this
environment, same situation Phase 3 hit with Sheets — `success.json`'s
`data` field was a literal placeholder, not valid base64, so this phase also
fixed that fixture). `app/services/rate_limiter.py` is a Redis fixed-window
counter (`provider:gemini:tokens:{minute}`) shared across workers; a window
at capacity routes through the same `RATE_LIMITED` path as a live 429.
`app/services/generation_service.py::transform_photo` runs one sub-job
end-to-end: Mode A (real-photo) completes straight to `COMPLETED`; Mode B
(synthetic) success lands in `QA_REVIEW` unscored — Phase 9 owns actual
scoring, this phase deliberately does not preempt it. `SAFETY_REFUSAL`
rejects immediately with no retry; transient classes get up to 3 in-process
attempts (a documented simplification — not literal Celery
`autoretry_for`/backoff, see `phases/phase-6-generation-worker.md`). A cost
event is written for every provider call, including refusals. Added
`config_versions.payload.global.unit_cost_usd` (schema gap: nothing priced
a Gemini call before this). `app/workers/generation.py` is a thin
session-lifecycle wrapper — nothing calls it yet (Phase 7's fan-out is what
wires a real job to it); a job created via `/generate` still sits at
`PENDING` forever until then, which is correct, not a regression.

Phase 7 — Orchestration & Partial Success is **complete**: `POST /generate`
now dispatches `orchestration.fan_out_job`, which marks the job `PROCESSING`
and calls `generation.transform_photo` for every non-skipped angle — a loop
of independent dispatches, not a Celery group/chord (`docs/conventions.md`
requires parent-status recompute in the same transaction as the sub-job
transition that triggered it, which a chord's out-of-band callback doesn't
match; see `phases/phase-7-orchestration.md`). `transform_photo` now sets
`GENERATING` before calling the provider (a Phase 6 gap — it previously left
the row at stale `PENDING` for the whole call) and recomputes+persists the
parent's status via `status_rollup.compute_parent_status` right after its
own terminal write, in the same transaction. A job created via `/generate`
now actually runs to `COMPLETED`/`PARTIAL_SUCCESS`/`FAILED` (or `PROCESSING`
if a synthetic angle is awaiting Phase 9's QA decision) instead of sitting
at `PENDING` forever. Wiring this up surfaced a real bug, not anticipated
when this phase was planned: a sync Celery task's `asyncio.run()` can't run
from inside `/generate`'s own async event loop under `task_always_eager`,
and a naive fresh-loop-per-call fix broke the Redis client singleton across
loops. Fixed with `app/workers/_async_utils.py::run_async`, which routes
such calls onto one persistent background loop; both worker task wrappers
also now build their own DB engine per call from `settings.DATABASE_URL`
read live rather than a shared engine bound at import time, for the same
cross-loop reason. `tests/conftest.py` gained an autouse fixture that fakes
Gemini success by default, since every `/generate` test now cascades into
real generation execution.

Phase 8 — Failure Taxonomy & Retry is **complete**. Failure classification,
in-process bounded backoff, and `REJECTED` handling were already fully built
by Phase 6/7 — re-verified, not re-built. This phase's actual scope was the
one piece every prior phase note pointed at:
`POST /jobs/{job_id}/angles/{angle}/retry` (`app/api/v2/retry.py`) is now
real — the `MOCK_MODE` gate is gone. `app/services/retry_service.py::execute_retry`
resets the sub-job to `PENDING`, moves a terminal job back to `PROCESSING`
immediately (mirroring `orchestration_service.dispatch_job`'s own eager
write), and records a `RETRY_REQUESTED` `JobEvent`; the route then dispatches
the exact `generation.transform_photo` primitive Phase 7 built. Three real
gaps found while wiring this up, all fixed: (1) `/retry` needed its own
Redis-only `retryidem:` idempotency namespace (`app/core/idempotency.py`) —
reusing `/generate`'s `idem:` prefix would let a client collide the two by
reusing one key value; (2) `app/services/status_service.py`'s `retryable`
flag didn't check `attempt_count` against the ceiling, so a maxed-out
sub-job still advertised a `retry_url` that would immediately 409 — fixed;
(3) confirmed (not assumed) that `generation_service.transform_photo`'s own
attempt loop already increments `attempt_count` regardless of dispatch
source, so the retry endpoint must not increment it again itself. A
pre-existing, not-fixed consequence found and documented rather than
silently patched: `generation_service.MAX_ATTEMPTS` and
`job_service.MAX_RETRY_ATTEMPTS` are both 3 and share one column
(`docs/schema.md`), so a sub-job that fails via the real internal retry loop
is already at the client-retry ceiling the moment it lands on `FAILED` — a
literal reading of `docs/business-rules.md` §5's shared-counter design, not
a bug, but worth flagging for whoever next tunes these constants. Also found
and fixed, unrelated to retry logic itself but exposed by finally exercising
it: `app/core/idempotency.py`'s Redis client was a module-level singleton
cached across calls — safe for `/generate` only because its real dedup path
never actually touched Redis (Postgres `jobs.payload_hash` since Phase 2;
the Redis `get_replay`/`store_replay` functions were dead code, now
replaced by the `/retry`-specific functions), but `/retry` finally exercised
it and hit `RuntimeError: Event loop is closed` across pytest's per-test
event loops — fixed by building a fresh client per call, matching the
per-call-engine pattern Phase 7 already established for exactly this class
of cross-loop bug. `scripts/upload_seed_assets.py` also backfills real bytes
for seeded `FAILED` sub-jobs' `INPUT` assets now, not just `COMPLETED`
`OUTPUT` assets — a real retry dispatch downloads the input, and a seeded
fixture's fabricated `storage_path` 404s the same way an un-backfilled
`COMPLETED` output did in Phase 1.

Phase 9 — Output QA Gate is **complete**: `app/providers/qa_base.py`
(`QaProvider`/`QaResult`, mirrors `GenerationProvider`) and
`app/providers/gemini_qa.py::GeminiQaProvider` implement an LLM-judged
similarity score via Gemini — the option `docs/ai-integration.md` had
already flagged as the likely default, decided here rather than adding a
second, dedicated embedding model. `app/services/qa_service.py::score_synthetic_angle`
is now real automatic scoring, wired into the pipeline for the first time:
`app/workers/generation.py` dispatches `qa.score_similarity` right after a
`QA_REVIEW`-landing `transform_photo` call commits (not from inside the
service, so a QA dispatch never reads a sub-job row before its own creating
transaction lands — same placement reasoning as `orchestration.fan_out_job`,
Phase 7). A gap not covered by any existing doc, found and closed here:
`docs/business-rules.md` §7 only ever described a successful score
landing above or below threshold — nothing about the QA provider call
itself failing. Decided and documented: a QA provider failure (timeout,
malformed response) fails open to a human — `QA_REVIEW`/`qa_status:
FLAGGED`, `qa_score: NULL` — never auto-`COMPLETED`, never
auto-`REJECTED`, matching the same "only mechanism that catches a silent
failure" reasoning `docs/ai-integration.md` already gives for the gate as a
whole. `GET /qa/review-queue` and `POST /qa/{sub_job_id}/decision` are both
real now — the review queue is scoped to `QA_REVIEW`+`qa_status: FLAGGED`
specifically (narrower than "all `QA_REVIEW`," matching
`docs/api-routes.md`'s own wording, not the roadmap line's looser phrasing),
with a real signed `image_url` and the job's **pinned** `config_version`'s
`reference_image_urls` (never the currently-active version — same rule
`/retry` already follows and for the same reason). Two new `ErrorCode`
values added (`SUB_JOB_NOT_FOUND`, `QA_NOT_PENDING`) since nothing existing
fit either case without misleading wording. `generation_service._recompute_parent_status`
was renamed `recompute_parent_status` (dropped the leading underscore, no
behavior change) so `qa_service.py` could reuse it rather than duplicating
the rollup-and-persist logic Phase 7 already built. **Not done, deliberately:**
`qa_similarity_threshold` (`0.82`) is still an uncalibrated placeholder — no
real client pieces exist in this environment to calibrate against (roadmap
open decision #8, still open). No `cost_events` row is written for a QA
call either — neither `docs/business-rules.md` §10 nor
`docs/ai-integration.md` ever described QA scoring as billed, so this phase
didn't invent that rule; flagged for Phase 11 to revisit with real usage
data if it turns out to matter.

Phase 10 — Auth & Security Hardening is **complete**, scoped narrower than
its roadmap line: real auth (Phase 1), input sanitization (Pydantic
`extra="forbid"` + Phase 4's image validation), and URL scoping were already
correct — re-verified, not re-built. What was real and missing: `POST
/generate` now enforces real per-client rate limiting and daily quotas —
`app/core/ratelimit.py::allow` (a Redis fixed-window counter) and
`api_clients.daily_job_quota` had both existed since Phase 0/1 with nothing
calling them; `docs/api-routes.md` already documented the `429` +
`Retry-After` contract this satisfies for the first time.
`app/db/repositories/jobs.py::count_created_today` is Postgres-backed, not a
new Redis key — matches the system-of-record decision already made for
everything else client-visible. An idempotent replay never consumes a token
or quota slot (same reasoning as it never billing the provider twice).
**Found and fixed, not part of the original scope:** `app/core/ratelimit.py`
had the exact same cached-module-global-Redis-client bug Phase 8 found and
fixed in `app/core/idempotency.py` — it was simply never exercised by any
test until this phase wired `allow()` into a route real tests actually hit.
Fixed the same way: a fresh client per call. Also added: a static
secret-leakage audit test (`tests/unit/test_secret_logging.py`) that
mechanizes the logging rule `docs/conventions.md` already stated in prose,
and a URL-scoping regression test confirming a `404` response body for
another client's job never leaks a storage path or signed URL. **Explicitly
not built, and why:** key rotation has no route anywhere in
`docs/api-routes.md` — inventing one would mean deciding undocumented API
surface (self-service vs. ops-only, grace period, in-flight-request
handling) rather than implementing a spec; added as roadmap open decision
#9 instead. A pen-test pass needs a live deployment, which doesn't exist
yet (Phase 1's own open item) — revisit after Phase 12.

Phase 12 — CI/CD & Deployment is **config/pipeline complete, live deploy
not verified** — a fundamentally different kind of "done" than every phase
before it, stated plainly rather than glossed over: this session has no
Fly.io or Upstash account and cannot create one on the user's behalf, so
nothing here has actually run against live infrastructure. What's real:
`fly.toml` (production) and `fly.staging.toml` (staging) define three Fly
process groups from the existing `Dockerfile` — `app` (uvicorn), `worker`
(`celery -Q io`), `beat` — with `[deploy] release_command = "alembic
upgrade head"` (migrations run before traffic shifts to a new release,
failing the deploy if the migration fails) and an HTTP health check against
the real `GET /api/v2/health` (Phase 0, already checks both Postgres and
Redis). `.github/workflows/deploy.yml` adds `deploy-staging` (every push to
`main`) and `deploy-production` (only a pushed `v*` tag — cutting a tag is
a deliberate human decision to ship) as new jobs gated on the existing `CI`
workflow's success via `workflow_run`, not a duplicated check list — a red
CI run can never trigger a deploy. `ci.yml` gained a `tags: ["v*"]` trigger
so it actually runs (and can succeed or fail) on a tag push, which
`workflow_run` depends on existing to gate against.
`docs/deployment.md`'s secrets checklist is mechanically cross-checked
against `app/config.py::Settings` by `tests/unit/test_deployment_docs.py`
— every field must appear in the doc, so the two can't silently drift
apart the way hand-maintained checklists usually do. **The target was
decided directly with the user, not guessed**: Fly.io + Upstash, both with
genuine ongoing free tiers at this project's likely volume — Render's free
tier doesn't cover Celery's background workers, Railway's is a one-time
credit. **GPU host provisioning (the roadmap line's other half) is moot,
not deferred** — `docs/decisions/0001-drop-local-matting.md` already
removed every VRAM-bound workload; roadmap open decision #3 is resolved
N/A. **What only the user can do, listed as such rather than claimed
done:** create the Fly.io and Upstash accounts, run `fly apps create` for
both apps, set `FLY_API_TOKEN` as a GitHub Actions secret, set every
`fly secrets set` value `docs/deployment.md` lists, and push — only then
does a real deploy, a real health check, or a real rollback become
possible to verify.

**Correction, same session:** Fly.io turned out to require a credit card
on every account (a 2024 policy change), found only when the user actually
tried to sign up. Added a second, cardless path: **Render + Upstash**,
matching V1 (`jewellery-gen-backend`)'s own already-live production setup,
found while investigating an unrelated question about where V1's secrets
lived. Render's free tier has no free Background Worker and no free
managed Redis, unlike Fly — solved with `scripts/render_start.sh` (runs
`alembic upgrade head`, backgrounds `celery beat` + `celery worker -Q io`,
then runs `uvicorn` in the foreground — one process tree, one billed
service, no application-code change) and `render.yaml`. Unlike the Fly
path, **this one was actually verified**: built the real Docker image, ran
it locally with real Supabase credentials and local Redis, confirmed via
`docker top` all three processes were genuinely running, and confirmed
`GET /api/v2/health` returned a real `200 {"status": "ok", ...}`. Both
paths are kept side by side (`docs/deployment.md` / `docs/deployment-free-
tier.md`, mirroring V1's own split), neither replacing the other. One
real, minor gap found and left open rather than fixed: the container runs
Celery as root, which Celery itself warns about at startup — fine for a
free-tier demo, worth a non-root user before real client traffic.
**Unrelated but important: a docker test-run command in this session
printed the real Supabase database password into the conversation
transcript in plaintext** (an inline-comment-stripping bug in an ad-hoc
`sed` command, not application code) — flagged to the user immediately,
who should rotate that Supabase database password.

Phase 13 — Load, Soak & Capacity Tuning: **tooling built and verified
against a real local server, live-deployment numbers not yet measured** —
confirmed with the user up front that this phase cannot be completed this
session, the same way Phase 12 couldn't fully verify Fly. `scripts/load_test.py`
(`httpx` + `asyncio`, no new load-testing framework) has a burst mode
(`--concurrency`/`--requests`) and a soak mode (`--soak-duration-minutes`,
samples `GET /api/v2/health` every minute), both actually run against a
real local API + Celery worker + real Supabase — not just parsed. That run
**found a real, currently-live bug**: the active `config_versions` row on
the shared Supabase project (`version_number=2`) predated Phase 6's
`unit_cost_usd` field addition, so every real `/generate` call was
crashing with an uncaught `KeyError` inside `transform_photo` rather than
failing cleanly — the script correctly recorded this as `TIMEOUT` (the job
never reached a terminal status) instead of crashing itself, which is
itself a legitimate validation of the tool's error handling. **Fixed
correctly, not by mutating the broken row** — `docs/conventions.md`'s hard
rule "Never mutate a `config_versions` row. New sync means new version"
applied directly here, so the fix inserted a new `version_number=3` row
with the corrected payload, deactivated version 2, and invalidated the
Redis `config:active` cache, mirroring exactly what `config_sync_service.py`
does on a real sync. Re-verified live afterward: a fresh `/generate` call
progressed to real `GENERATING` instead of crashing (it still won't
complete, since no real `GEMINI_API_KEY` exists in this environment — a
different, already-known limitation, not this bug). `docs/capacity-tuning.md`
documents `IO_QUEUE_CONCURRENCY`, the global-vs-per-client Gemini
rate-limit relationship (nothing in code enforces one budget can't starve
another), and an Upstash free-tier command-budget estimate (~10
commands/real-photo-job, ~50,000 jobs/month ceiling) — every number
labeled estimated, none claimed as measured. VRAM saturation (the
roadmap's own wording) is N/A, not deferred — no GPU workload has existed
since decision 0001. **Unrelated cleanup, same session:** a stray
`load-test-client` `api_clients` row from this verification work was
deactivated rather than deleted (its `jobs` rows are FK-referenced, and
this schema never deletes job history anyway) — it'll show up in any
future `GET /jobs` query once Phase 11 builds that route for real.
