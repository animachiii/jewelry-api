# Phase 16 — Stability Closeout (Reconciliation, Task Time Limits, Storage Audit)

## Reality check before writing this

This phase was **not** written by a session with access to this repo's own history — it was written by a separate Cowork session that connected directly to the live Render service (`jewelry-api`, `srv-d9s46ifavr4c73ae6oc0`) and the live Supabase project (`rsolykmjupiusdujajgj`) on 2026-08-15 and read what was actually running, then cloned this repo afterward to check its findings against the real code. That ordering matters: some of what it found live turned out to already be fixed in code; this phase file only claims what's still true after checking both.

**Already fixed, confirmed by reading the code — not this phase's job to redo:**

- `app/core/redis_client.py::new_redis_client` — the "event loop is closed" bug that orphaned every angle after the first one in a job at `GENERATING` (2026-08-12), and the missing `REDIS_SOCKET_TIMEOUT_SECONDS` that could hang the whole single-worker queue on a dead TCP connection (2026-08-13). Both fixed, both documented in that file's own docstring.
- `scripts/render_start.sh` — switched to `--pool=solo` on 2026-08-13 specifically because the forked prefork child (~154MB, mostly `google-genai`) plus everything else didn't fit Render's 512MB free tier. The script's own comment shows the measured before/after.
- The live Render memory metrics (queried directly, 2026-08-15) confirm this landed: usage before the 2026-08-13 18:16 UTC deploy peaked 460–525MB against a 536MB limit with ~35 container restarts across 6 days; after that deploy, usage sits around 232–355MB. **The memory-pressure OOM cycle is resolved.**
- **Row Level Security being disabled on all 8 tables is a deliberate, documented decision** (`migrations/versions/0001_initial_schema.py`, `docs/schema.md`), not an oversight — the backend connects with the Supabase service role and is the only writer; the Flutter ERP never touches Postgres directly, and nothing in this codebase distributes the anon key to any client. The live Supabase advisory that flags this as "critical" is a generic linter rule; it doesn't know this architecture. **Verified directly** as part of this phase, not re-fixed — see Step 3.

**Still real, still open — what this phase actually does:**

1. **No backstop exists for a hung or crashed task.** `GEMINI_REQUEST_TIMEOUT_SECONDS` and `REDIS_SOCKET_TIMEOUT_SECONDS` bound the two specific calls that have already caused incidents, but `celery_app.conf` sets no `task_time_limit` / `task_soft_time_limit` at all — any other kind of hang (a worker OOM-killed mid-task, an unhandled exception path that doesn't reach `generation_service`'s own error handling, a future bug of the same shape as the two already found) has no ceiling and leaves the sub-job wherever it died.
2. **Nothing reconciles a sub-job that's already stuck.** Queried live on 2026-08-15: 15 `ANGLE_GENERATION` sub-jobs sitting in `PROCESSING`/`GENERATING`-equivalent status, the oldest since 2026-08-07 — created before the two bugs above were fixed, and never cleaned up because no code path does that cleanup. `app/workers/retention.py` sweeps **assets**; nothing sweeps **jobs**.
3. **Supabase Storage is at 484MB of the 500MB free-tier ceiling.** `jewelry-outputs` alone holds 39,328 objects totalling 19MB — averaging ~500 bytes per object, which is not a real image. That needs an explanation before Phase 17 puts this project in front of AWS billing and a bigger, more expensive version of the same unexplained growth.
4. **`OUTPUT` asset retention is still unset** (roadmap open decision #5) — `app/services/retention_service.py`'s `RETENTION_DAYS` dict already supports a value the moment one is chosen; nothing else needs to change.

This phase closes 1, 2, and 4, and produces a real answer to 3. It deliberately does **not** touch AWS — Phase 17 depends on this phase specifically so a bug doesn't get migrated to a more expensive box. Everything in this phase is checkable **live**, against the real Render service and real Supabase project, the same way this session verified the reality-check claims above — this is not a fixture-driven phase.

---

## Step 1 — Task time limits

### What to do

In `app/workers/celery_app.py`, add hard and soft time limits to `celery_app.conf`, sized against the two calls already known to be the longest legitimate work a task does:

```python
celery_app.conf.task_time_limit = 180        # hard kill
celery_app.conf.task_soft_time_limit = 150    # SoftTimeLimitExceeded raised first
```

Reasoning for the numbers: `GEMINI_REQUEST_TIMEOUT_SECONDS` defaults to 120, plus `REDIS_SOCKET_TIMEOUT_SECONDS` (5) at two or three call sites, plus real margin for image upload/download against Supabase Storage. 150/180 gives a legitimate slow job room to finish while still bounding a genuine hang to 3 minutes instead of forever.

Then, in the task bodies that do real work (`app/workers/generation.py`, `app/workers/background.py`), catch `celery.exceptions.SoftTimeLimitExceeded` at the outermost level, and route it through the **same** failure-handling path `generation_service`/`background_service` already use for a caught provider error — set `sub_job.failure_class = FailureClass.INTERNAL`, a message naming the timeout, write the `job_events` row via `job_events_repo.record_event` (same pattern `retry_service.execute_retry` already uses), and recompute parent job status via the existing `status_rollup` logic. Do not introduce a new failure class — `INTERNAL` already exists and fits.

### Checkpoint 1

- [ ] `celery_app.conf.task_time_limit` and `task_soft_time_limit` are set and visible in `celery_app.conf` at import time — assert this in a unit test so a future refactor can't silently drop them
- [ ] A test that makes a task body sleep past `task_soft_time_limit` (via `time_limit=` override on `.apply()` in eager mode, or a short override via `pytest` fixture) results in the sub-job landing on `FAILED` / `FailureClass.INTERNAL` with a message naming the timeout, and a `job_events` row recording it — not an uncaught exception
- [ ] Parent job status recomputes correctly after the timeout — a job with one timed-out sub-job and the rest `COMPLETED` lands on `PARTIAL_SUCCESS`, matching `docs/business-rules.md` §2's existing rollup rule
- [ ] Deployed to the live Render service and confirmed present: trigger a real job, inspect that the worker process's Celery config reflects the new limits (log the effective config at worker startup if not already logged)

---

## Step 2 — Reconciliation sweep

### What to do

New `app/services/reconciliation_service.py`, same shape as `app/services/retention_service.py` (testable directly against testcontainers Postgres, no Celery dependency in the logic itself):

```python
async def reconcile_stuck_sub_jobs(session: AsyncSession, stale_after_seconds: int) -> int:
    """Find sub_jobs in a non-terminal status whose most recent job_events
    row is older than stale_after_seconds, mark them FAILED / INTERNAL with
    a message naming this as a reconciliation action (not a real provider
    failure), write the job_events row, and recompute each affected parent
    job's status via the existing status_rollup logic. Returns count reconciled.
    """
```

Use `job_events` for the staleness check, not `sub_jobs.updated_at` if that column doesn't exist — confirm against `docs/schema.md` which timestamp is actually queryable per sub-job, and correct this phase file's assumption here if it's wrong, per this repo's own convention of writing against reality rather than the plan.

New `app/workers/reconciliation.py`, identical structure to `app/workers/retention.py`:

```python
@celery_app.task(name="reconciliation.sweep_stuck_sub_jobs")
def sweep_stuck_sub_jobs() -> dict[str, int]:
    reconciled = asyncio.run(_run())
    return {"reconciled": reconciled}
```

Add to `celery_app.conf.beat_schedule` alongside `asset-retention`, on a new `RECONCILIATION_SWEEP_CRON` setting in `app/config.py` (default every 15 minutes — frequent, since a stuck job is a client-visible symptom, not a housekeeping concern like asset expiry). `stale_after_seconds` should be comfortably longer than Step 1's `task_time_limit` (e.g. 600s) — the sweep is a backstop for jobs the time-limit mechanism itself failed to catch (a worker OOM-killed outright leaves no task to raise `SoftTimeLimitExceeded`), not the primary mechanism.

**Then, separately, run the one-time cleanup of the 15 legacy orphans that predate both this sweep and the bugs that created them.** Do this through the same service function with a short-lived script (`scripts/reconcile_legacy_orphans.py`) that calls `reconcile_stuck_sub_jobs` with a cutoff that only catches jobs from before 2026-08-13 18:16 UTC (the deploy that fixed the underlying causes) — do not silently sweep anything created after that point without checking it individually first, since a job legitimately `PROCESSING` right now would be wrongly killed by an overly broad one-time cutoff.

### Checkpoint 2

- [ ] `reconcile_stuck_sub_jobs` is unit-tested against testcontainers Postgres: a sub-job with a stale `job_events` timestamp is marked `FAILED`/`INTERNAL`, a fresh one is left untouched, and the parent job status recomputes correctly for both single-sub-job and multi-sub-job jobs
- [ ] The Celery beat schedule includes the new task, verified the same way `asset-retention`'s presence is checkable — `celery_app.conf.beat_schedule` contains `reconciliation-sweep`
- [ ] Run against the **real live Supabase database** (this session already has read/write access via the connected Supabase tool — use it to verify, don't just trust the test suite): before the one-time cleanup script runs, confirm the count of stuck `ANGLE_GENERATION` sub-jobs is 15; after it runs, confirm it's 0, and confirm each affected job's `status` correctly rolled up to `FAILED` or `PARTIAL_SUCCESS` depending on its other sub-jobs
- [ ] Confirm no sub-job created after 2026-08-13 18:16 UTC was touched by the one-time script — cross-check the affected `sub_job_id` list against `created_at`
- [ ] `select count(*) from sub_jobs where status not in ('COMPLETED','FAILED','REJECTED','SKIPPED') and <staleness condition>` returns 0 immediately after the sweep runs, live

---

## Step 3 — RLS verification (not remediation)

### What to do

This step exists to close the loop on a claim, not to change anything. Confirm, and write the confirmation down:

1. The Supabase connection string used by `DATABASE_URL` in every deployed environment uses the **service role**, not the anon role — check the actual connection string format in the Render dashboard's Environment tab (or wherever it's set for the live service) and confirm it's the service-role/session-pooler credential `docs/deployment-free-tier.md` specifies, not the anon key.
2. Grep the entire repo, `ui/index.html` included, for any occurrence of a Supabase anon key or `SUPABASE_ANON_KEY` — confirm zero results (this session already ran this grep against the code and found none; re-run it as part of this phase's self-audit so it's checked at the point of the actual deploy, not just once).
3. Confirm the Flutter ERP's integration guide (`docs/integration-guide.md`) does not instruct the client's team to talk to Supabase directly for anything other than **signed URLs the backend itself generates** — presigned upload URLs and signed output URLs are safe regardless of RLS, because they're scoped, time-limited grants issued by the service-role backend, not open anon-role table access.
4. If all three hold, add one line to `docs/schema.md`'s existing RLS note: *"Verified 2026-08-XX: no anon-role credential exists in any deployed environment or client-facing code. The Supabase advisory flagging this as critical does not apply to this architecture."* If any of the three does **not** hold — an anon key turns up anywhere — stop and treat that as a real incident, not a documentation update: rotate the key immediately via the Supabase dashboard and only then close this step.

### Checkpoint 3

- [ ] Live Render environment variable for `DATABASE_URL` confirmed to use the service-role/session-pooler credential
- [ ] Grep for anon-key patterns across the full repo returns zero matches
- [ ] `docs/integration-guide.md` reviewed; no instruction anywhere tells the Flutter team to hold a Supabase credential
- [ ] `docs/schema.md` updated with the verification line and today's date, or an incident is opened and this checkpoint stays unchecked until resolved

---

## Step 4 — Storage audit

### What to do

Write `scripts/audit_storage.py`: connect to Supabase Storage, and for each of the three buckets (`jewelry-inputs`, `jewelry-outputs`, and the legacy `jewellery-gen` bucket if it's still reachable from this project), sample a statistically meaningful set of objects (all of them, if under a few thousand; a random 500 otherwise) and report: size distribution (min/p50/p95/max), content-type distribution, and for any object under 2KB, fetch and characterize it directly — is it a valid but tiny image, a JSON error payload, a zero-byte artifact, or something else. Cross-reference a sample against `assets.storage_path` to check whether every storage object has a corresponding row, and vice versa.

Given `jewelry-outputs`' 39,328 objects average ~500 bytes, the leading hypothesis worth checking first: a bug writing an error/placeholder object to the outputs bucket on a failed or QA-rejected generation, rather than only writing on real success. Check `app/services/generation_service.py` and `app/services/background_service.py` for every code path that calls the storage upload for an `OUTPUT`-kind asset, and confirm each one only runs after a real image is in hand.

Once the audit has a real explanation: if it's a bug, fix it and add a regression test. If it's legitimate (e.g., intentional thumbnail or QA-preview objects), document it in `docs/schema.md`'s storage section so it's not re-flagged as an anomaly next time someone looks at this bucket. Either way, report the resulting real bucket sizes.

Then close roadmap open decision #5: propose an `OUTPUT` retention value — **180 days** absent a client answer, following this repo's own convention of not inventing an unstated business decision but also not leaving a real capacity risk fully unbounded — set it in `app/services/retention_service.py`'s `RETENTION_DAYS` dict, and mark open decision #5 in `phases/phase-roadmap.md` as **defaulted, not resolved**, with a note that the client can change one dict value at any time.

### Checkpoint 4

- [ ] `scripts/audit_storage.py` run against the real live buckets; a written report (`docs/storage-audit-2026-08.md` or similar) states the actual explanation for the `jewelry-outputs` object-count anomaly, not a guess
- [ ] If a bug was found, it's fixed with a regression test; if not, `docs/schema.md` documents why the object count/size profile is expected
- [ ] Every `assets` row has a corresponding storage object and vice versa, or the discrepancy count is reported and explained
- [ ] `RETENTION_DAYS['OUTPUT']` is set to a real integer, not `None`; the retention sweep (`app/workers/retention.py`, already live) begins expiring `OUTPUT` assets older than that value on its next scheduled run
- [ ] Total live storage usage across all buckets, measured after this step, is reported — target under 350MB (70% of the free-tier ceiling); if still above that, the retention value needs to be more aggressive than 180 days, decided from the real number, not guessed twice

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file.
2. Test each one for real. This phase is unusual in that most of its checkpoints **can** be verified against the actual live Render service and live Supabase project — this session (or the Claude Code session executing this phase) should use that access rather than settling for fixture-driven proof where live proof is available.
3. Return a structured report:
   ✅ [Checkpoint] — Pass
   ⚠️ [Checkpoint] — Partial: [specific reason]
   ❌ [Checkpoint] — Fail: [specific reason]
4. Fix all failures and partials before reporting phase complete.
5. Update `docs/schema.md` (RLS verification line, retention value, storage anomaly explanation), `app/config.py`'s env var list if `RECONCILIATION_SWEEP_CRON` was added, `phases/phase-roadmap.md` (mark decision #5 defaulted; add this phase's row), and `CLAUDE.md` if anything here changed an architectural decision worth a reader knowing without opening this file.
6. Only say "Phase 16 Complete" when every checkbox is green, the live system reflects it, and docs are in sync.

## Final Phase 16 Checklist

- [ ] Task time limits set and proven to fail a task safely into `FAILED`/`INTERNAL` rather than hanging forever
- [ ] Reconciliation sweep live in the Celery beat schedule; the 15 pre-existing stuck sub-jobs are reconciled for real, verified against live Supabase
- [ ] RLS confirmed intentional and safe, or an incident opened and resolved if it isn't
- [ ] Storage anomaly explained with evidence, not assumed; `OUTPUT` retention set to a real value; total usage measured and reported
- [ ] Self-audit passed with all green
- [ ] `docs/`, `app/config.py`, `phases/phase-roadmap.md`, and `CLAUDE.md` updated to match what was actually built
- [ ] Manual verification done by architect

---

## Note for Phase 17

Phase 17 (AWS Deployment) is sequential after this one specifically because migrating an unreconciled, unexplained system to new infrastructure just moves the same open questions somewhere more expensive to debug. Phase 17 should start from a live Render service with zero stuck jobs, a storage number under control, and Celery config that already includes the time limits and reconciliation sweep this phase adds — the AWS deployment then just needs to carry that same, now-correct configuration forward, not invent new stability work on top of a move.
