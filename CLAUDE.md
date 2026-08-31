# Project Constitution — AI Jewelry Generation API (v2)

## Project Overview

A headless, asynchronous REST API that turns client-supplied jewelry photographs into
catalog-ready product imagery. A single request submits up to four camera angles
(FRONT, SIDE, DIAGONAL, TOP) for one jewelry category; the backend mattes each subject
locally, sends it to Gemini for background synthesis, relighting, and shadow generation,
and returns per-angle image URLs. Jobs succeed partially by design — three good angles
and one failure is a normal, fully-supported outcome.

**Correction, 2026-08-09:** the "no UI in this repository" decision above no
longer holds literally. `ui/index.html` is a single-file demo/test client —
presign → upload → generate → poll, plus a browser for the 8 seeded demo
jobs — added after the first live Render deploy specifically to verify the
real pipeline end-to-end without waiting on the Flutter team. It mirrors V1
(`jewellery-gen-backend`)'s own `ui/index.html` precedent and the same
framing: **not the ERP integration**, shown in the page's own header. Served
same-origin via a `StaticFiles` mount at `/ui` in `app/main.py` — required
because, unlike V1, this API has no CORS setup at all (no `docs/business-
rules.md` §R21 equivalent), so a browser page can only reach it from the
API's own origin. The Flutter ERP remains the only real consumer; nothing
about its contract changed.

**Correction, 2026-08-12 (Phase 15):** the overview above describes only the
four-angle flow — that's no longer the whole API. `POST /background/remove`
and `POST /background/replace` are a second, independent request shape: one
uploaded photo in, one image out, no category or angles, reusing the same
job/sub-job state machine, status polling, retry, cost recording, and audit
trail rather than a parallel pipeline. Both go through the same Gemini call
Mode A angle generation already uses — **no alpha channel**; "background
removal" means the product on a flat/solid backdrop, not a transparent
cutout (a decision made directly with the client rather than through the
planned spike — no real Gemini key or client photos existed locally to run
one; see `docs/decisions/0002-background-removal-approach.md`). See
`docs/business-rules.md` §13 and `docs/api-routes.md`'s "Background
Operations" section for the full contract.

**Correction, 2026-08-16 (Phase 18):** a fourth operation family now exists
alongside the four-angle flow and the two background operations above:
`POST /match` (`MATCH`) generates 1-4 companion-piece variants from one
uploaded photo, used as a *style reference* rather than the subject being
transformed — the output is a different physical piece, not the same one
restaged. Reuses the same job/sub-job state machine, status polling, cost
recording, and audit trail as everything above; ships straight to
`COMPLETED` with no QA gate (a deliberate scope decision, not an oversight
— see `docs/business-rules.md` §7/§14). Also generalized
`POST /jobs/{job_id}/retry` — previously background-operations-only, always
retrying a single sub-job — to retry every `FAILED` sub-job on a job,
all-or-nothing; verified a behavioral no-op for the existing background
case. See `docs/business-rules.md` §14, `docs/api-routes.md`'s
"Companion-Piece Generation" section, and `docs/ai-integration.md`'s Mode D
for the full contract.

**Correction, 2026-08-16 (Phase 19):** a fifth operation family now exists:
`POST /recolor` (`RECOLOR`) recolors a masked gemstone region to a palette
color from one uploaded source photo plus one uploaded mask. First
operation needing a mask — the Gemini API has no mask parameter, so the
mask is conveyed as a colour overlay before the provider call and drives a
server-side compositing step afterward that discards everything the
provider changed outside the (feathered) mask. Unlike every other
operation, **the client-facing output is not the provider's raw
response** — it's the original source composited with the provider's
response, so everything outside the mask is byte-identical to the upload.
No QA gate, for a third, distinct reason from MATCH's (a pixel-exact
compositing-correctness question, not a similarity-score one). See
`docs/business-rules.md` §15, `docs/api-routes.md`'s "Masked Gemstone
Recolor" section, and `docs/ai-integration.md`'s Mode E for the full
contract.

**Correction, 2026-08-16 (Phase 20):** a sixth operation family now exists:
`POST /mix` (`MIX`) grafts a masked region from one uploaded piece
("secondary") into a masked region of another ("primary"), producing one
merged photo. Extends RECOLOR's mask-validation and generate-then-composite
machinery across two independent source/mask pairs, plus a genuinely new
step no prior operation needed: a deterministic rough-composite (crop
region B via its mask, scale/align it into region A via A's mask — no
provider call involved in placement) *before* any provider call, followed
by a refinement call scoped only to a ring around the graft's seam, not the
whole edited region — cross-image spatial reasoning is the weakest
capability in play for any current-generation image model, so placement is
never asked of Gemini. Like RECOLOR, **the client-facing output is not the
provider's raw response** — it's the rough-composite (itself already a
deterministic merge of two different pieces' photos) composited with the
provider's response, so everything outside the seam band is byte-identical
to the rough-composite. No QA gate, for a fourth, distinct reason from
MATCH's and RECOLOR's. This is the third and last currently-planned v3
feature phase — see `phases/phase-roadmap.md`'s "Deferred to v3" table. See
`docs/business-rules.md` §16, `docs/api-routes.md`'s "Two-Piece Masked
Merge" section, and `docs/ai-integration.md`'s Mode F for the full
contract.

**Correction, 2026-08-24 — post-Phase-20 memory incident:** live on the
Render free-tier `jewelry-api` deployment, a real RECOLOR request pushed
memory from a ~200MB baseline to 487MB (91% of the 512MB limit) in a
single sample, OOM-killing the container — confirmed via Render's own
metrics API and the automatic "exceeded its memory limit" email, not
inferred. Root cause: `recolor_service._build_overlay` and
`mix_service._build_seam_overlay` both decoded the client's full-resolution
upload with no size cap before compositing — a real jewelry photo can be
4000px+ on the long edge, and each decoded RGB buffer at that size is
tens of megabytes, with several held simultaneously (source, mask, eroded
mask, magenta fill, composite result). Fixed with a new
`settings.WORKING_MAX_EDGE` (default 2048px, `app/config.py`) applied to
both throwaway pre-provider-call overlays — neither is the client-facing
artifact, so downscaling them costs nothing correctness-wise; both
operations' actual final compositing steps
(`recolor_service._composite_result`, `mix_service._composite_seam_result`)
are untouched and still operate at full original resolution, preserving
the byte-identical-outside-the-mask/seam-band guarantee. **Not fixed in this
first pass, and flagged rather than silently left open:**
`mix_service._build_rough_composite` decoded four full-resolution images at
once (both sources, both masks) — its output is not throwaway, it's the
base the final composite is built on, unlike the two overlay functions this
fix addressed, so it needed separate treatment.

**Correction, 2026-08-28 — MIX's rough-composite had two real defects, found
on the first genuine client run that ever reached Gemini (job `fe7d6372`).**
Both are now fixed in `mix_service._build_rough_composite`. (1) `mask_b` was
used *only* to compute a bounding box and its actual shape discarded, so the
raw rectangular crop was grafted — on the real job mask B was two curved
bands whose shared bbox was just **39.5% painted**, meaning **60% of the
grafted content was unpainted mannequin**, which landed as a beige blob in
the middle of the client's pendant. (2) The scale-to-fit did not preserve
aspect ratio, so a tall thin region squashed into a wide box distorted
badly — `phases/phase-20-mix.md` called this "a deliberate simplification...
unvalidated against real client pieces", and this was the validation. Now:
aspect-preserving fit-inside, centred, pasted through the **intersection of
both silhouettes**. Consequently `_build_rough_composite` returns a **graft
mask** and the seam band is built from that rather than mask A — once the
graft is an intersection, mask A is no longer its boundary and banding mask A
would blend an edge that isn't there. **Still open:** MIX fits B's region
into A's region, so it works best when the two masked shapes are roughly
comparable; there is no ingest-time check for that.

**Correction, 2026-08-27 — MIX output is now resolution-capped, and the two
prior fixes are superseded.** Neither of the passes below was enough. A real
client MIX job (`baf56f78`, two 3072x4096 / **12.6 MP** photos plus two masks)
OOM-killed the container roughly three seconds in, on **every** attempt,
before a single Gemini call — the live sub-job sat at `GENERATING` with
`attempt_count` still `0`, and Celery's `acks_late=False` dropped the task on
each restart, so the job hung at `GENERATING` rather than failing. Measured at
that exact size: the pipeline needed **~187MB** against **~160MB** of headroom
(512MB cap, 353MB baseline). Fix: `mix_service._load_downscaled` now decodes
**all four** inputs at `WORKING_MAX_EDGE`, using Pillow's `draft()` for the
JPEG fast path and NEAREST for masks (so they stay binary, which
`_seam_band_mask` depends on). Measured result: **187MB -> 74MB**, output
1536x2048. **This gives up the full-resolution output guarantee** — decided
directly with the user, who chose it over restructuring for full resolution
or paying for a larger instance. §16's byte-identical-outside-the-seam-band
rule is unchanged in substance (it was always relative to `rough_composite`,
never to an original photo). **`RECOLOR` was deliberately not changed and
retains the same exposure** — its guarantee is stated against the untouched
original source, so its final composite still runs at full resolution.

**Correction, 2026-08-25 — MIX's remaining hotspot fixed:**
`_build_rough_composite` now downscales source B and mask B to
`WORKING_MAX_EDGE` before the crop — both were always going to be
cropped-then-resized into region A's bounding box regardless (the
non-aspect-preserving scale-to-fit §16 already documents), so decoding them
at native resolution bought nothing. Source A and mask A still decode at
full resolution — required, same guarantee as above — but the function's
own redundant `.copy()` of source A (a second full-resolution RGB buffer
with no purpose, since the original was never read again) is gone too. Net:
down from four uncapped full-resolution buffers to two. See
`docs/business-rules.md` §16's own incident notes and
`app/config.py`'s `WORKING_MAX_EDGE` comment for the full accounting.

**Actors:**

| Actor | Can do |
| :--- | :--- |
| Flutter ERP (API client) | Fetch config, request presigned uploads, submit jobs, poll status, retry a single failed angle |
| Showcase UI (`ui/index.html`) | Same API, same `client`-scope key. Demo/test only — see correction above |
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
    background.py           POST /background/remove, /replace (Phase 15 — not in the
                            original sketch). retry.py also gained a second route in
                            the same file, POST /jobs/{job_id}/retry, rather than a
                            new module — same "retry" concern, same router.
    schemas/               Pydantic request/response models, one file per route family
      background.py         BackgroundRemoveRequest/BackgroundReplaceRequest (Phase 15)
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
                            than duplicating the rollup-and-persist logic. Phase 15
                            adds score_background_operation (input photo as
                            reference, not the category matrix) sharing a private
                            _score_and_apply helper with score_synthetic_angle —
                            same provider call + pass/fail branching, only
                            threshold/reference-image resolution differs.
    background_service.py   process — single background-operation sub-job end to
                            end, mirrors generation_service.py::transform_photo's
                            shape (Phase 15 — not in the original sketch;
                            app/workers/background.py is a thin wrapper). Reuses
                            GeminiProvider unmodified — both operations are
                            already Gemini calls with a flat/solid background, no
                            new provider code needed
                            (docs/decisions/0002-background-removal-approach.md).
                            Unlike transform_photo, success always enters
                            QA_REVIEW, never straight COMPLETED. Also marks the
                            parent job PROCESSING at the start — a background job
                            has no orchestration.fan_out_job equivalent to do
                            that eagerly, since there's only ever one sub-job to
                            dispatch.
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
                            Phase 15 adds `qa.score_background`, dispatched by
                            background.py the same way, real logic in
                            qa_service.py::score_background_operation.
    orchestration.py       `orchestration.fan_out_job` — a loop of independent
                            generation.transform_photo dispatches, not a Celery
                            group/chord (Phase 7 — see phases/phase-7-orchestration.md's
                            reality-check section for why)
    background.py           `background.process` — session lifecycle only, real
                            logic in services/background_service.py (Phase 15).
                            Same per-call engine + per-call owned Redis client
                            pattern as generation.py, pinned by
                            tests/integration/test_background_worker_event_loop_lifecycle.py.
  providers/
    base.py                GenerationProvider ABC + GenerationResult (Phase 6)
    gemini.py               GeminiProvider — the only module that imports google.genai,
                            and only inside a deferred _call_api seam (Phase 6).
                            Reused unmodified by background_service.py (Phase 15) —
                            same one-photo-in/one-photo-out call shape.
    qa_base.py              QaProvider ABC + QaResult, mirrors base.py (Phase 9)
    gemini_qa.py             GeminiQaProvider — LLM-judged similarity scoring, same
                            deferred-import isolation as gemini.py (Phase 9). Reused
                            unmodified for the Phase 15 subject-preservation gate.
migrations/                Alembic. 0006-0010 are Phase 15: operation_t +
                            jobs.operation + nullable sub_jobs.angle (0006),
                            operations/background_presets config seed (0007),
                            nullable jobs.category_code (0008), jobs.preset_code
                            (0009), background_qa_similarity_threshold config
                            seed (0010).
scripts/
  seed_dev.py              Idempotent dev seed data — all 8 job-state scenarios.
                            CATEGORY_PAYLOAD's `global` block also seeds Phase 15's
                            operations/background_presets/
                            background_qa_similarity_threshold — real production
                            gets these via migrations 0007/0010, this script mirrors
                            them so a fresh dev/test DB has usable data too.
  export_openapi.py        Writes docs/openapi.json from the live FastAPI app
  upload_seed_assets.py    Backfills real placeholder bytes for seeded COMPLETED
                            output assets (Supabase's sign endpoint 404s otherwise)
tests/
  unit/  integration/  fixtures/
docs/                      Reference files imported below, plus:
  openapi.json              Committed OpenAPI 3.1 spec, diff-checked in CI
  integration-guide.md      Flutter ERP integration guide (Phase 1 deliverable)
phases/                    Phase specification files
ui/
  index.html                Single-file demo/test client, mirrors V1's own —
                            see the "no UI in this repository" correction
                            above. Served at /ui via a StaticFiles mount
                            (app/main.py). Never imported by app/ code.
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
- **A job's operation is explicit, not inferred from its shape.** `jobs.operation`
  (Phase 15) makes `ANGLE_GENERATION` vs `BACKGROUND_REMOVAL` vs
  `BACKGROUND_REPLACEMENT` a first-class column rather than something derived from
  `category_code` being null or `sub_jobs.angle` being null — those nullability facts
  are consequences of `operation`, not the source of truth for it.

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
6. **Never return a synthetic angle as `COMPLETED` without a QA score.** Same rule for
   every background-operation output (Phase 15) — unlike real-photo angles, a
   background operation has no unchecked path at all; success always enters
   `QA_REVIEW` first.
   **Amended 2026-08-30, decided directly with the user:** one exception, and only
   one — when the judge never rendered a verdict at all (every
   `QA_MAX_ATTEMPTS` exhausted on a transient provider failure, or a
   deterministic provider failure), the sub-job completes with
   `qa_status: NOT_APPLICABLE` and `qa_score: NULL` rather than entering the human
   queue. An unevaluated output is not a rejected one, and a judge outage was
   filling the review queue with good images nobody had actually looked at. **A
   real judge verdict below threshold still flags, always.** The accepted risk is
   explicit: while the judge is unreachable a genuinely drifted output ships
   unchecked. `QA_PASS_ON_PROVIDER_ERROR=false` restores the original rule without
   a code change. See `docs/business-rules.md` §7.
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

Phase 11 — Observability & Cost Tracking is **complete, scoped narrower than
the roadmap line** — decided directly with the user, 2026-08-16: Sentry
declined for cost reasons (its free Developer tier would likely cover this
project's current volume, but the user chose not to add another account
regardless), and Celery/queue-depth dashboards (Flower/Grafana) deliberately
not built, same "on client request only" reasoning `phases/phase-roadmap.md`'s
own "Deferred to v3" table already gives for a queue admin dashboard.
structlog correlation was already real since Phase 0/1 — not part of this
phase's actual scope despite the roadmap line naming it. What got built:
`GET /jobs` and `GET /jobs/{job_id}/cost` (`app/api/v2/jobs.py`), both real
for the first time — previously `raise NotImplementedError` stubs with
real schemas and ops-scope auth already wired since Phase 1. Job listing is
paginated and filterable by `status`/`category_code`/`created_after`/
`created_before`; cost reporting sums a job's `cost_events` with a
**derived**, not stored, `attempt_count` per sub-job (no such column exists
on `cost_events`). One real bug found and fixed while building, not
anticipated by the stub: `JobSummary.category_code` was typed non-optional,
but `jobs.category_code` has been nullable since Phase 15 — would have
crashed response validation on the first background/RECOLOR/MIX job. Per-SKU
cost aggregation explicitly not built — no API contract for it exists in
`docs/api-routes.md`, unlike the two routes this phase built. No new
migration. See `phases/phase-11-observability-cost-tracking.md`.

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

Phase 15 — Standalone Background Operations is **complete, verified against
testcontainers Postgres + real local Redis + real Supabase Storage,
fixture-driven Gemini** (same stack as every prior phase — no real
`GEMINI_API_KEY` exists in this environment, so real-world output quality
is unverified; see the Step 1 note below). `BACKGROUND_REMOVAL` and
`BACKGROUND_REPLACEMENT` are real: `POST /background/remove`,
`POST /background/replace`, operation-aware `POST /uploads/presign`,
`GET /status/{job_id}`'s additive `operation`/`results` fields, and
`POST /jobs/{job_id}/retry` (409 on an angle job) are all live and tested
end-to-end, including the real Celery dispatch chain
(`background.process` → `qa.score_background`) running under
`task_always_eager` exactly the way `/generate`'s already does.

**Step 1's spike never ran.** The phase file required a timeboxed
comparison over ≥12 real client pieces before any code was written — this
environment has no real `GEMINI_API_KEY`, no Vertex-capable service
account, no hosted-matting-API credentials, and (unlike every prior phase,
which at least had fixture image bytes) zero image files anywhere in the
repo. Surfaced to the user directly rather than silently skipped; the user
decided directly, without the spike, that both operations go through
Gemini with a flat/solid background — no alpha channel, no transparent
cutout. See `docs/decisions/0002-background-removal-approach.md` for the
full accounting of what evidence Checkpoint 1 asks for and wasn't
gathered.

**Two real bugs found and fixed while wiring the worker up, not caught by
code review alone** (same self-audit discipline this file's own template
asks every phase to follow):

1. `background_service.process` never marked the parent job `PROCESSING`
   when work started. Angle jobs get this for free from
   `orchestration_service.dispatch_job` (a separate task that runs before
   any `generation.transform_photo` dispatch); a background job has no
   fan-out step — it's always exactly one sub-job, dispatched directly by
   `create_background_job_for_request` or the retry route. Without the
   fix, a job that landed in `QA_REVIEW` below threshold would stay at
   `PENDING` forever instead of `PROCESSING`. Revert-checked: removing the
   fix made `test_below_threshold_qa_score_lands_in_qa_review_and_review_queue`
   fail exactly as expected.
2. `QaReviewItem` (`GET /qa/review-queue`) required non-null `angle` and
   `category_code` — the route crashed with a real `500` the first time a
   background item reached it. Fixed by making both nullable, adding
   `operation`, and populating `reference_image_urls` with the input
   photo's signed URL for a background item (its "reference" is the
   original photo, not a category matrix — without this a human reviewer
   would see a subject-preservation item with nothing to compare the
   output against).

**Also found and fixed, unrelated to the two bugs above:** `app/core/errors.py`'s
`RequestValidationError` handler crashed with a `500` on *any*
`@model_validator` raising a bare `ValueError` — including the
already-shipped `AngleSpec` mode validator on `/generate`, just never
exercised through a real HTTP request before Phase 15 added
`PresignUploadRequest`'s own mode validator. Pydantic puts the raised
exception object itself in the error's `ctx.error`, which plain
`json.dumps` (what `JSONResponse` uses) can't serialize. Fixed with
`fastapi.encoders.jsonable_encoder`, revert-checked, regression test
added (`tests/unit/test_errors.py`).

**Schema gaps found and closed, not anticipated by the phase file:**
`jobs.category_code` had to become nullable (migration 0008) — a
background job has no category at all, and the phase file only ever
addressed `sub_jobs.angle`. `jobs.preset_code` (migration 0009) had to be
added — nothing durable recorded which backdrop preset a
`BACKGROUND_REPLACEMENT` job requested (the phase file's own
`create_background_job_for_request` sketch only put it in the
`JOB_CREATED` audit-log detail, not a queryable column), so the worker had
no way to resolve the right prompt at execution time.

**Two placeholder config values seeded, both explicitly uncalibrated,**
same status `qa_similarity_threshold` has always carried: per-operation
prompts/costs and the single `STUDIO_WHITE` preset (migration 0007), and
`background_qa_similarity_threshold` (migration 0010, `0.92` — deliberately
higher than the synthetic-angle gate's `0.82`, on the phase file's own
reasoning that "same object, new background" should score closer to 1.0
than a novel view). Real preset list and real threshold calibration are
still open (roadmap open decisions #8 and #11).

**Not done, and can't be from this session:** Step 5's "measured end-to-end
latency on the live free instance" — no reachable Render deployment;
Step 6's Flutter-lead written confirmation of the `operation`/`results`
split — needs an actual human session, same category of gap Phase 1's own
sign-off had.

**Addendum, 2026-08-13 — custom-background compositing:** `BACKGROUND_REPLACEMENT`
now also accepts an uploaded background photo (`background_storage_path`) as
an alternative to `preset_code`, composited via the same single Gemini call
with a second reference image appended
(`app/providers/gemini.py::GeminiProvider.generate` already accepted a
`reference_images: list[bytes]` — no provider change needed). New nullable
`sub_jobs.background_asset_id` (migration 0011) and a seeded
`custom_background_prompt` (migration 0012, placeholder/uncalibrated content —
same status as `qa_similarity_threshold` and the other Phase 15 seed values
above). Does not reopen decision 0002 — still no alpha channel, still one
flattened image out.

Phase 16 — Stability Closeout is **complete**, verified live against the
real Render service (`srv-d9s46ifavr4c73ae6oc0`) and real Supabase project
(`rsolykmjupiusdujajgj`) — not fixture-driven, per the phase file's own
instruction. Not in the original 15-phase plan; added after a live
diagnostic session found the 2026-08-13 free-tier OOM fix already landed in
code but its symptoms never cleaned up.

- **Task timeout enforcement had to change mechanism, not just get
  configured.** `celery_app.conf.task_time_limit`/`task_soft_time_limit`
  (180s/150s) are set, but confirmed **inert** under this deployment's
  `--pool=solo` (the 2026-08-13 OOM fix) — read directly from the installed
  `celery==5.6.3` source: solo's `TaskPool` reports `timeouts: ()`, and its
  `on_apply` (`concurrency.base.apply_target`) silently discards a
  `timeout`/`soft_timeout` kwarg without ever enforcing it. Real enforcement
  is `settings.WORKER_TASK_TIMEOUT_SECONDS` (180s) applied via
  `asyncio.wait_for` inside `app/workers/generation.py` and
  `app/workers/background.py`, catching both `TimeoutError` and (for free,
  in case the pool ever changes back) `SoftTimeLimitExceeded`. Both route to
  new `generation_service.mark_sub_job_timed_out`.
- **Reconciliation sweep** (`app/services/reconciliation_service.py`,
  `app/workers/reconciliation.py`, beat task `reconciliation.sweep_stuck_sub_jobs`,
  every `RECONCILIATION_SWEEP_CRON` — default 15 min, deliberately frequent
  since this deployment's container restarts every 1-4 hours on the free
  tier, confirmed via Render Events) is scoped to **`PENDING`/`GENERATING`
  only, never `QA_REVIEW`** — the phase file's original sketch ("any
  non-terminal status") would have wrongly failed sub-jobs correctly
  waiting in the human review queue (`docs/business-rules.md` §7); caught
  by checking real live data before writing the sweep, not by inspection
  alone. One-time cleanup (`scripts/reconcile_legacy_orphans.py`)
  reconciled 23 real pre-2026-08-13 orphaned sub-jobs live (not 15 — the
  live count had grown since the diagnostic session that wrote this phase's
  spec); every affected job's status rolled up correctly, verified live.
- **Found and fixed, not anticipated by the phase file:**
  `app/workers/retention.py` carried the exact same shared-engine +
  bare-`asyncio.run()` shape `app/workers/config.py`'s own docstring already
  documents as a past production incident (`RuntimeError`, connection bound
  to a closed loop on a second same-process tick) — found while building
  the new reconciliation worker on the same beat-task shape, not by
  auditing retention on purpose. Fixed to the same per-call-engine +
  `run_async` pattern `config.py` already established.
- **RLS verification (not remediation):** confirmed, not assumed —
  `DATABASE_URL` uses the Postgres session-pooler role (`postgres.<project-ref>`),
  not an anon/authenticated JWT role; zero anon-key matches repo-wide;
  `docs/integration-guide.md` never tells the Flutter team to hold a
  Supabase credential. See `docs/schema.md`'s RLS note for the full
  verification record.
- **Storage audit found a different anomaly than expected.** The leading
  hypothesis (a code bug writing placeholder objects on failed
  generations) was wrong — confirmed by reading every OUTPUT-asset write
  path. Real cause: this repo's own test suite (deliberately never mocking
  Storage, per `docs/ai-integration.md`) uploads real bytes to the real,
  shared Supabase project on every run with nothing ever cleaning them up.
  99.7-99.9% of objects in both `jewelry-inputs`/`jewelry-outputs` had no
  matching `assets` row. Fixed going forward with a new autouse pytest
  fixture (`tests/conftest.py::_cleanup_storage_uploads`); the existing
  57,022-object backlog (176MB) was deleted live after explicit
  confirmation. A third bucket, `jewellery-gen` (308MB), is V1's and was
  never touched — see `docs/storage-audit-2026-08.md` for the full
  accounting, including the corrected note that this bucket, not V2's own
  usage, is the larger long-term capacity constraint (Supabase's quota is
  project-wide, not per-bucket — worth surfacing again before Phase 17).
- **A related gap found while wiring up the `OUTPUT` retention default:**
  `_complete_success` in both `generation_service.py` and
  `background_service.py` never passed `expires_at` to `create_asset` at
  all, so every `OUTPUT` asset was `NULL` regardless of
  `RETENTION_DAYS[AssetKind.OUTPUT]` — setting that value would have been
  silently inert without this fix. `RETENTION_DAYS[AssetKind.OUTPUT]` is
  now **180 days** (defaulted, not resolved — roadmap open decision #5),
  driven by the real 484.6MB-of-500MB pressure this audit found, not by an
  actual client answer; it remains one dict entry the client can change at
  any time.
- **Not done, and can't be from this session:** a Flutter-lead or
  ops-side written sign-off that the new failure/reconciliation behavior
  is acceptable — same category of gap every prior phase's sign-off item
  carries.

Phase 17 — AWS Deployment (App Runner) is **pipeline/config complete, no
real AWS account behind it yet** — same category of "done" Phase 12's Fly
path always was. Client-mandated move off Render's free tier
(`docs/decisions/0003-deploy-to-aws.md`) after the crash history Phase 16
fixed at the code level; AWS App Runner was chosen over ECS Fargate or EC2
because it runs the existing container (`scripts/render_start.sh`)
**unchanged** — no VPC, no task definitions, no service split. Everything
session-provable is verified: `.github/workflows/deploy-aws.yml` is valid,
OIDC-authenticated (a real improvement over the Fly path's static
`FLY_API_TOKEN`), gated on CI via the same `workflow_run` pattern
`deploy.yml` already uses; `docker build .` against the unmodified
`Dockerfile` succeeds and the resulting image's `CMD` is still
`./scripts/render_start.sh`, confirmed live, not assumed;
`docs/deployment-aws.md`'s secrets table is cross-checked field-by-field
against `app/config.py::Settings`, including a note that
`IO_QUEUE_CONCURRENCY` stays inert under `--pool=solo` on this path too,
same as every other path, unless a future capacity decision reintroduces
prefork. **Render stays primary** (`docs/deployment-free-tier.md`) until
App Runner is verified live — that verification is entirely the user's
remaining step (AWS account, ECR repositories, OIDC IAM role, App Runner
service — see `docs/deployment-aws.md`'s "What only the user can do"), the
same honesty split Phase 12's Fly path always carried and never closed.
