# Phase 7 — Orchestration & Partial Success

## Reality check before writing this

Phase 6 built `generation.transform_photo` (via `app/services/generation_service.py`)
— a Celery task that runs one sub-job's generation call end-to-end and writes its
terminal state. **Nothing calls it.** A job created via `POST /generate` sits at
`PENDING` forever; `GET /status` already reads real data (Phase 1/2) but there's
nothing to read except the initial `PENDING` rows. This phase closes that loop.

**Gap found in Phase 6, closed here:** `docs/business-rules.md` §2's sub-job state
machine is `PENDING → GENERATING → QA_REVIEW → COMPLETED` (or straight to
`COMPLETED`). Phase 6's `transform_photo` never actually set `GENERATING` — it left
the row at `PENDING` for the whole call and only wrote a terminal status at the end.
A client polling mid-generation would see `PENDING`, not `GENERATING`. Fixed in Step 1.

**Deviation from the roadmap's literal wording, decided here:** the roadmap phrase is
"Celery group fan-out, chord rollup." `docs/conventions.md` states explicitly: "Parent
status recomputation and the sub-job transition that triggered it happen in the same
transaction" — a chord callback firing once after the whole group finishes is a
*different* design than that rule describes (a chord recomputes once, out-of-band,
after the fact; the conventions rule wants every single transition to carry its own
recompute, in its own transaction, immediately). Given the explicit rule already in
the docs, this phase implements recompute-per-transition instead of a chord: fan-out
is a loop of independent `generation.transform_photo.delay(sub_job_id)` calls (no
`celery.group` primitive needed since nothing here waits on the group as a unit), and
`transform_photo` itself recomputes and persists parent status in the same
transaction as its own sub-job's terminal write. This is simpler, avoids Celery
chord's well-documented flakiness under `task_always_eager`, and is closer to what
the docs actually specify than the roadmap's shorthand. Documented here rather than
silently diverging, same as Phase 6's retry-loop deviation.

**Test blast radius, decided here:** wiring `/generate` to actually dispatch work
means every existing `/generate` integration test now cascades into real generation
execution under `task_always_eager=True` (already the global test setting). Rather
than leave `/generate` inert in tests, `tests/conftest.py` gets a new autouse fixture
that fakes `GeminiProvider._call_api` to the `success.json` fixture by default —
every `/generate` call in every test now genuinely completes a job, which is more
realistic than the previous "creates a job that sits at PENDING forever" test
behavior. The one existing test whose assertions depended on the old inert behavior
(`test_generate_real.py::test_happy_path_creates_job_sub_jobs_asset_and_event`) is
updated to assert the new, real terminal state instead of `PENDING`. Two other
pre-existing tests needed small fixes exposed by the same cascade: `test_ingest_pipeline.py`
picked up an extra `OUTPUT` asset row it hadn't accounted for, and this phase's own
new tests initially used placeholder (non-decodable) upload bytes that Phase 4's real
image validation now correctly rejects — both fixed, not worked around.

**A real bug found and fixed while wiring dispatch into `/generate`, not anticipated
when this phase was planned:** `.delay()` under `task_always_eager` executes a task's
body inline. `POST /generate` is itself an async route handler with its own running
event loop, and both worker task wrappers used a plain `asyncio.run()` internally —
which raises `RuntimeError: asyncio.run() cannot be called from a running event loop`
the moment dispatch happens from inside that handler. This never surfaced in Phase 6
because nothing called `generation.transform_photo` from an async context yet.
Fixed with `app/workers/_async_utils.py::run_async`, which detects the
already-in-a-loop case and routes the coroutine onto a single persistent background
loop instead of a fresh one per call — a fresh loop per call was tried first and
broke differently: `app/core/redis_client.py`'s connection is a process-wide
singleton, and asyncio/asyncpg/redis connections aren't shareable across event loops,
so a multi-angle job (multiple cascaded `transform_photo` calls, each getting its own
throwaway loop) corrupted that singleton after the first call. The same reasoning
applies to the two worker modules' DB access: both now build a fresh SQLAlchemy
engine per call from `settings.DATABASE_URL` read live, instead of importing the
shared `app.db.session.async_session_factory` bound at process start — tests
redirect this by monkeypatching `settings.DATABASE_URL` to the testcontainers
container (`tests/conftest.py`'s `db_session` fixture), the same pattern
`tests/integration/test_migrations.py` already used for the same reason.

---

## Step 1 — Close the GENERATING gap

### What to do

`app/services/generation_service.transform_photo`: immediately after loading the
sub-job (before building the prompt or calling the provider), set
`sub_job.status = SubJobStatus.GENERATING`, `sub_job.started_at = now()`, and
`session.commit()` that alone — so a concurrent `GET /status` mid-call sees
`GENERATING`, not `PENDING`. The rest of the function continues in a fresh logical
unit (still the same session; SQLAlchemy allows further work after a commit).

### Checkpoint 1

- [x] ⚠️ Partial: the `GENERATING` write and its immediate `session.commit()`
      happen unconditionally before the provider call (code-verified), and every
      resulting sub-job's `started_at` being set confirms the write executed — but
      no test actually asserts a *concurrent* read observes `GENERATING` mid-call
      the way the original wording promised. Doing that properly needs a second
      session reading while the first is paused inside the (fixture-faked) provider
      call, which none of this phase's tests set up. Not re-scoped down in the
      checkpoint text above because the gap is worth keeping visible for whoever
      next touches this function.

---

## Step 2 — Parent-status recompute on every sub-job transition

### What to do

`app/services/generation_service.transform_photo`: after writing the sub-job's
terminal status (success or failure path), in the **same transaction**:

1. Load all sub-jobs for the parent job.
2. `R` = count where `status != SKIPPED`, `S` = count `COMPLETED`,
   `F` = count in (`FAILED`, `REJECTED`) — matching `docs/business-rules.md` §3
   exactly (already implemented and tested in `app/services/status_rollup.py` since
   Phase 2 — this phase is the first to actually call it).
3. `new_status = status_rollup.compute_parent_status(R, S, F)`.
4. Update `job.status`, `job.succeeded_angles`, `job.failed_angles`. Set
   `job.completed_at = now()` when `new_status` is terminal (`COMPLETED`,
   `PARTIAL_SUCCESS`, `FAILED`); leave it `NULL` while `PROCESSING`.
5. Write a `JobEvent` (`SUBJOB_STATUS_CHANGE`, `from_status`/`to_status` on both the
   sub-job and, if it changed, the parent).
6. Commit.

`QA_REVIEW` sub-jobs count as neither `S` nor `F` (per §3 — the parent stays
`PROCESSING` while any sub-job awaits a QA decision), which `compute_parent_status`
already handles correctly by construction (not counted in either bucket).

### Checkpoint 2

All verified in `tests/integration/test_orchestration.py` against real
testcontainers Postgres + real Supabase Storage, fixture-driven Gemini:

- [x] All angles succeed → parent `COMPLETED`, `completed_at` set —
      `test_all_angles_succeed_parent_completed`
- [x] Mixed success/failure → parent `PARTIAL_SUCCESS` —
      `test_mixed_success_and_failure_parent_partial_success`
- [x] All angles fail → parent `FAILED` — `test_all_angles_fail_parent_failed`
      (also confirms `attempt_count == MAX_ATTEMPTS` after exhausting retries)
- [x] Single-angle job that fails → parent `FAILED`, not `PARTIAL_SUCCESS`
      (`docs/business-rules.md` §3's explicit "gets wrong" case) —
      `test_single_angle_failure_is_failed_not_partial_success`
- [x] A synthetic angle landing in `QA_REVIEW` keeps the parent `PROCESSING`, even
      if every other angle already succeeded —
      `test_qa_review_keeps_parent_processing_even_with_other_successes`
- [x] `SKIPPED` sub-jobs never affect `R`, `S`, or `F` —
      `test_skipped_sub_jobs_never_affect_rollup`
- [x] Recompute and the triggering sub-job write are the same DB transaction — true
      by construction (both happen inside `transform_photo` before its single
      `session.commit()` in `app/workers/generation.py`'s `_run`); not separately
      tested by forcing a mid-transaction crash, which would need fault injection
      this phase didn't build

---

## Step 3 — Fan-out dispatch

### What to do

`app/services/orchestration_service.py`: `dispatch_job(session, job_id)` —
sets `job.status = PROCESSING`, `job.started_at = now()` (only if still `PENDING`;
idempotent against a double-dispatch), commits, then returns the list of sub-job IDs
eligible for generation (`status == PENDING`, i.e. every non-`SKIPPED` angle — none
are `SKIPPED` and simultaneously eligible, `SKIPPED` is set once at creation and never
transitions per `docs/business-rules.md` §2).

`app/workers/orchestration.py`: `orchestration.fan_out_job(job_id: str)` — thin
Celery task (session lifecycle only, same split as every worker task since Phase 4):
calls `dispatch_job`, then `generation.transform_photo.delay(sub_job_id)` for each
returned ID. No `celery.group`/`chord` — see the reality-check section above for why.

`app/api/v2/generate.py`: after `create_job_for_request` successfully creates a new
job (not on a replay — replaying an idempotency key must not re-dispatch), call
`fan_out_job.delay(str(job.id))`.

### Checkpoint 3

- [x] A freshly created job reaches `PROCESSING` — confirmed as the starting state
      every terminal-status test in `test_orchestration.py` observes (by the time
      the eager cascade finishes and the test reads the row, dispatch has already
      happened; `orchestration_service.dispatch_job`'s own `PENDING`-only guard is
      the code-level guarantee this happens exactly once, before any sub-job result
      lands)
- [x] Every non-skipped sub-job gets a `transform_photo` dispatch; skipped angles
      never do — `test_skipped_sub_jobs_never_affect_rollup` (a skipped angle
      staying `SKIPPED`, never `PENDING`→terminal, is only possible if it was never
      dispatched)
- [x] `dispatch_job`'s idempotency guard (`if job.status == JobStatus.PENDING`) is
      code-verified, not exercised by a dedicated duplicate-trigger test
- [x] An idempotent `/generate` replay does not re-dispatch —
      `test_idempotent_replay_does_not_redispatch` (`job.started_at` unchanged
      across the replay)

---

## Step 4 — Wire it up end-to-end, real `GET /status` progression

### What to do

With Steps 1–3 done, `POST /generate` → real job creation → real dispatch → real
per-sub-job execution → real parent-status recompute is now a closed loop. Verify
`GET /status` reflects it accurately at every stage, using the existing (Phase 1/2)
real status-read path — no changes needed there, only verification.

### Checkpoint 4

- [x] End-to-end: `POST /generate` with one real-photo angle → poll `GET /status` →
      `COMPLETED`, real signed `image_url` — `test_generate_real.py::test_happy_path_creates_job_sub_jobs_asset_and_event`.
      `retryable: false` on a `COMPLETED` angle specifically confirmed in the mixed
      test below (same code path)
- [x] End-to-end: a job with a mix of angles engineered to succeed and fail →
      `PARTIAL_SUCCESS`, the failed angle's `retryable: true` +
      correct `retry_url`, the succeeded angle's `retryable: false` + `retry_url: null` —
      `test_orchestration.py::test_mixed_success_and_failure_parent_partial_success`
- [x] End-to-end: a synthetic angle → `QA_REVIEW`, parent stays `PROCESSING`,
      `image_url` is `null` on that angle —
      `test_generate_real.py::test_happy_path_creates_job_sub_jobs_asset_and_event`
      (`DIAGONAL` angle) and
      `test_orchestration.py::test_qa_review_keeps_parent_processing_even_with_other_successes`

---

## Step 5 — Self-audit

Same discipline as every prior phase: re-read every checkpoint above, verify with
real tests (testcontainers Postgres, real local Redis, real Supabase Storage,
fixture-driven Gemini), fix failures before declaring done, sync `docs/schema.md` /
`docs/business-rules.md` (if the chord deviation needs a note there) /
`docs/ai-integration.md` / `CLAUDE.md` / `phases/phase-roadmap.md`.

---

## Note for Phase 8

Phase 8 (Failure Taxonomy & Retry) makes `POST /jobs/{job_id}/angles/{angle}/retry`
real — it currently stays `MOCK_MODE`-gated. Real retry execution will reset a
`FAILED` sub-job to `PENDING`, increment `attempt_count`, and dispatch a fresh
`generation.transform_photo.delay(sub_job_id)` — the exact same dispatch primitive
this phase built, reused rather than reinvented. The parent-status recompute this
phase added to `transform_photo` already handles a job moving back from a terminal
state to `PROCESSING` on a successful retry dispatch (`docs/business-rules.md` §2:
"a successful retry can move `PARTIAL_SUCCESS` or `FAILED` back to `PROCESSING`" —
`compute_parent_status` already returns `PROCESSING` whenever `S + F < R`, which is
true the moment a previously-terminal sub-job goes back to `PENDING`/`GENERATING`).
