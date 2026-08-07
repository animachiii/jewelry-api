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
BiRefNet-matting on local GPU · Gemini Image API · Docker Compose · Sentry + structlog

## Folder Structure

```
app/
  main.py                 FastAPI app factory, middleware, router registration
  config.py               Pydantic Settings — all env vars, single source
  api/v2/                 Route handlers only. No business logic.
    config.py  generate.py  status.py  retry.py  uploads.py  health.py
  core/
    auth.py               API key verification dependency
    errors.py             Exception classes + handlers, error envelope
    logging.py            structlog setup, job_id correlation
    ratelimit.py          Redis token bucket
    idempotency.py        Idempotency-Key storage and replay
  db/
    session.py            SQLAlchemy async engine + session factory
    models/               ORM models, one file per table group
    repositories/         All queries live here. Routes never query directly.
  services/
    config_service.py     Sheets sync, version snapshot, Redis cache
    job_service.py        Job creation, state machine, partial-success rollup
    storage_service.py    Supabase Storage upload/download/signed URLs
    cost_service.py       Cost event recording
  workers/
    celery_app.py         Celery config, queue routing, beat schedule
    health.py              ping_gpu/ping_io verification tasks (added Step 4, not in original plan)
    matting.py            GPU queue: BiRefNet alpha matte extraction
    generation.py         IO queue: Gemini calls
    qa.py                 IO queue: perceptual similarity gate
    orchestration.py      Group fan-out, chord callback, parent rollup
  providers/
    base.py               GenerationProvider ABC
    gemini.py             Gemini implementation
migrations/               Alembic
tests/
  unit/  integration/  fixtures/
docs/                     Reference files imported below
phases/                   Phase specification files
```

## Key Architectural Decisions

- **Postgres is the system of record. Redis is not.** Redis holds the Celery broker,
  the config cache, rate-limit buckets, and idempotency keys. Every one of those is
  reconstructible from Postgres or Sheets. Nothing is lost if Redis is flushed.
- **GPU and IO work run on separate Celery queues.** Matting is VRAM-bound and needs
  concurrency 1–2 per card; Gemini calls are network-bound and want concurrency 20+.
  Sharing a pool starves one or OOMs the other.
- **The matting model loads once per worker process,** at `worker_process_init`, never
  inside a task. Under prefork, loading in-task multiplies VRAM by the child count.
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
3. **Never load an ML model inside a Celery task body.** Process-init only.
4. **Never write business logic in a route handler.** Routes validate, delegate to a
   service, and serialize. Nothing else.
5. **Never query the database from a route or a task directly.** Go through
   `db/repositories/`.
6. **Never call the live Gemini or Sheets API in tests.** Use recorded fixtures.
7. **Never return a synthetic angle as `COMPLETED` without a QA score.**
8. **Never accept a `/generate` request without checking the `Idempotency-Key`.**
   Duplicate submissions bill the client twice.
9. **Never fail a job because Google Sheets was unreachable.** Fall back to the last
   active `config_versions` row in Postgres.
10. **Never log raw API keys, Supabase service keys, or full signed URLs.**
11. **Never delete an asset row.** Soft-expire via `expires_at`; storage lifecycle
    handles the bytes.
12. **Never mutate a `config_versions` row.** New sync means new version.

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

Phase 0 — Foundation & Environment. Steps 1, 2, 3, 4, 6, 7 are complete and
verified live against real infrastructure: repo pushed to
github.com/animachiii/jewelry-api (public, branch-protected, CI green),
schema migrated to the real Supabase Postgres (session pooler, port 5432)
with all constraints and `alembic check` drift-free, Storage buckets live
(jewelry-inputs/mattes/outputs, private, signed-URL round trip verified),
and seed data loaded showing all 8 job scenarios with correct parent
status. Step 5 (matting benchmark) is the only remaining blocker — needs
GPU access and real client jewelry photos. Check
`phases/phase-0-foundation.md` for the itemized checkpoint status and
`phases/phase-roadmap.md` before starting any work.

`app/workers/health.py` was added during Step 4 for the `health.ping_gpu`/
`health.ping_io` verification tasks — it isn't in the folder structure below
because Step 4 asked for these tasks without specifying a file and neither
belonged in `matting.py` or `generation.py`.
