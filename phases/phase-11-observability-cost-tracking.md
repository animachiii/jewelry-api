# Phase 11 — Ops Job Listing & Cost Reporting (scoped)

## Reality check before writing this

**Scope decided directly with the user, 2026-08-16, narrower than this phase's
original roadmap line** ("Sentry, structlog correlation, Celery/queue metrics,
dashboards, alerting, per-job and per-SKU cost reporting"). Sentry costs money
past a free tier the user didn't want to commit to for a project this size, and
this codebase's own "Deferred to v3" table already gives the standing reasoning
for not building a Flower/Grafana queue dashboard speculatively: "covers ~90% at
~5% of the cost... build the GUI only on client request." Nobody has asked for
one. Applying that same reasoning here rather than re-litigating it: **this phase
builds real per-job cost reporting and real ops job-listing/filtering — the two
pieces of "observability" that already have a committed API contract and stubbed
route waiting for them — and explicitly does not add Sentry, Celery/queue metrics,
or a dashboard.**

**What's already real, confirmed by reading the code, not assumed from the
roadmap line:** `app/api/v2/jobs.py`'s two routes (`GET /jobs`, `GET
/jobs/{job_id}/cost`) exist, are wired into `app/main.py`, have real Pydantic
response schemas (`app/api/v2/schemas/jobs.py`), and are gated by
`require_ops_scope` — **but both bodies are `raise NotImplementedError(...)`**,
each explicitly saying "lands in Phase 11." `structlog` correlation
(`request_id`/`job_id` contextvars) is **already built and live** — Phase 0/1's
own work, not part of this phase's scope, confirmed via `app/core/logging.py`
and `app/core/middleware.py`. `cost_service.record_cost_event` already writes one
`cost_events` row per provider call attempt, including refusals, and every
operation's service module already calls it — **the data this phase reports on
already exists**; nothing changes about how cost events are recorded.

**One real gap found while reading the target schema against the current
`jobs` table, not anticipated by the original route stub:** `JobSummary.category_code`
is typed `str` (non-optional). That was true when `ANGLE_GENERATION` was the only
operation. Since Phase 15, `jobs.category_code` is nullable — background
operations, RECOLOR, and MIX all have `category_code: NULL`; only
`ANGLE_GENERATION` and MATCH (which reuses the column for `target_category`) set
it. A literal implementation of the current schema would crash Pydantic
validation the first time `GET /jobs` returned a background/RECOLOR/MIX row. This
phase fixes the type as part of making the route real — see Step 1.

**Second gap, same category:** `docs/api-routes.md` documents `GET /jobs` as
"filterable by status, category, and date range," but the route stub's own
signature only has `status`/`category_code`/`page`/`page_size` — no date
parameters exist yet. This phase adds `created_after`/`created_before` to match
the already-documented contract, rather than quietly shipping a narrower filter
set than what's written down.

**No schema migration needed.** Every column this phase reads
(`jobs.*`, `cost_events.*`, `sub_jobs.angle`) already exists. This is the first
phase since Phase 10 with zero new Alembic revisions.

---

## Step 1 — `GET /api/v2/jobs`: real job listing

### What to do

1. Fix `app/api/v2/schemas/jobs.py::JobSummary.category_code` to
   `str | None` — matches `jobs.category_code`'s real nullability
   (`docs/schema.md`). Also add `operation: Operation` to the summary — an ops
   list mixing five operation types with no way to tell them apart from the
   outside is not a usable list; every other ops-facing surface in this codebase
   (`GET /qa/review-queue`, `GET /status/{job_id}`) already surfaces `operation`
   for the same reason.
2. New `app/db/repositories/jobs.py::list_jobs` — paginated query over `jobs`,
   **unscoped by client** (ops sees every client's jobs, unlike every
   client-scoped query this repository otherwise has), filtered by:
   - `status: JobStatus | None`
   - `category_code: str | None`
   - `created_after` / `created_before: datetime | None` — matches
     `docs/api-routes.md`'s already-documented "date range" filter, not
     previously implemented
   Ordered `created_at DESC` (newest first — the useful default for an ops
   triage view), with a `count()` query for `total` alongside the page query
   (two queries, not a window function — this table has an existing
   `(client_id, created_at DESC)` index but no ops-wide one; a window-function
   `COUNT(*) OVER()` would force a full scan either way at this data volume,
   and two simple queries are easier to reason about and test independently).
3. Real route body in `app/api/v2/jobs.py::list_jobs` — calls the repository
   function, maps `Job` rows to `JobSummary`, returns `JobListResponse`.

### Checkpoint 1

- [ ] `GET /jobs` with no filters returns every job across every client,
      newest-first, respecting `page`/`page_size`
- [ ] `status` filter returns only jobs in that status
- [ ] `category_code` filter returns only jobs with that category
- [ ] `created_after`/`created_before` filter correctly bound the date range
- [ ] A job with `category_code: NULL` (background/RECOLOR/MIX) appears in an
      unfiltered list without a validation error — the specific bug this
      phase's own reality check found
- [ ] `total` reflects the full filtered count, not just the current page's size
- [ ] A `client`-scope key gets `403`, not `404` or `200` — this route has
      never been reachable by a client key; confirm the existing
      `require_ops_scope` dependency actually enforces that once the body is
      real, not just when it 500'd on `NotImplementedError`

---

## Step 2 — `GET /api/v2/jobs/{job_id}/cost`: real cost reporting

### What to do

1. New `app/db/repositories/cost_events.py` — `get_by_job(session, job_id)`
   returning every `CostEvent` row for a job, ordered `created_at ASC`, joined
   to `sub_jobs` for `angle` (the response schema wants it; `cost_events` itself
   has no `angle` column, only `sub_job_id`).
2. **`CostEventItem.attempt_count` has no backing column** — `cost_events` was
   never given one (`docs/schema.md`); `sub_jobs.attempt_count` is a single
   current-value counter, not a per-event history. Derive it instead: number
   each sub-job's own cost events in creation order (1st call for that sub-job
   is attempt 1, 2nd is attempt 2, ...) via a `ROW_NUMBER() OVER (PARTITION BY
   sub_job_id ORDER BY created_at)` in the same query — this is exactly what
   "attempt count at the time of this call" means, and it's derivable
   losslessly from data that already exists, so no new column is justified
   (`docs/conventions.md`'s migration discipline: don't add a column for
   something a query can already answer).
3. Real route body in `app/api/v2/jobs.py::get_job_cost` — `404` if the job
   doesn't exist (ops has no client-scoping to hide behind, so this is a plain
   not-found, not the client-scoped `404`-not-`403` masking `GET /status`
   uses), otherwise sums `total_cost_usd` across all events and returns
   `JobCostResponse`.

### Checkpoint 2

- [ ] `GET /jobs/{job_id}/cost` on a job with N provider-call attempts across
      one or more sub-jobs returns N `CostEventItem`s and a correct
      `total_cost_usd` sum
- [ ] A sub-job retried twice shows `attempt_count: 1, 2` on its two events, in
      call order — the derived-numbering logic, not a stored value
- [ ] A failed/refused call's cost event is included — `docs/business-rules.md`
      §10: "a refused generation is still billed," and this report must not
      silently drop it
- [ ] A job with events from more than one sub-job (e.g. two angles) returns
      events correctly attributed to each angle via the `sub_jobs` join
- [ ] A job with **no** cost events (e.g. failed before any provider call) returns
      `total_cost_usd: 0`, `events: []`, not a `404` or a crash
- [ ] Unknown `job_id` returns `404`
- [ ] A `client`-scope key gets `403`

---

## Explicitly not built this phase

- **Sentry / error tracking** — the user declined it for cost reasons; Sentry's
  free Developer tier would likely cover this project's current volume, so this
  is a deliberate deferral, not a technical blocker. Revisit if error volume or
  team size ever makes the free tier's limits (5,000 events/month, 1 project) a
  real constraint.
- **Celery/queue-depth metrics, Flower, Grafana** — same standing reasoning the
  roadmap's own "Deferred to v3" table already gives for a queue admin
  dashboard: build only on client request, not speculatively.
- **Per-SKU cost aggregation** — the roadmap line names it, but no API contract
  for it exists anywhere in `docs/api-routes.md` (unlike the two routes this
  phase builds, which were already speced and stubbed). Inventing that surface
  now would mean deciding undocumented API shape rather than implementing a
  spec — same category of gap Phase 10 flagged for key rotation. If wanted,
  scope it as its own follow-up against a real decision about what grouping/
  time-window ops actually needs, not guessed here.
- **Alerting** — no destination exists to alert into without Sentry or an
  equivalent; moot until that decision is revisited.

---

## Self-Audit Instruction

1. Re-read every checkpoint above.
2. Test each one for real against `testcontainers` Postgres — no fixture data,
   no mocking; every job/sub-job/cost-event row created via the real service
   functions this phase reuses unmodified.
3. Return a structured report (✅/⚠️/❌ per checkpoint).
4. Fix all failures and partials before reporting complete.
5. Update `docs/api-routes.md` (the two routes are already documented at the
   contract level — confirm the built behavior matches exactly, including the
   `created_after`/`created_before` params this phase adds) and
   `phases/phase-roadmap.md`'s Phase 11 row to reflect the real, scoped-down
   status — do not claim the full original roadmap line ("Sentry... dashboards,
   alerting...") is done.

## Final Phase 11 Checklist

- [ ] `GET /jobs` and `GET /jobs/{job_id}/cost` both real, no `NotImplementedError`
      left anywhere in `app/api/v2/jobs.py`
- [ ] `JobSummary.category_code` nullability bug fixed and tested
- [ ] Date-range filter gap (documented but unimplemented) closed
- [ ] Derived `attempt_count` numbering tested against a real multi-attempt
      sub-job, not assumed correct from the SQL alone
- [ ] Self-audit passed with all green
- [ ] `docs/api-routes.md`, `phases/phase-roadmap.md` updated to reflect the
      real, narrower scope — Sentry/metrics-dashboard/per-SKU explicitly noted
      as not done, not silently dropped
