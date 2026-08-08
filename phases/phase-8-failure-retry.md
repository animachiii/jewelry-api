# Phase 8 — Failure Taxonomy & Retry

## Reality check before writing this

The roadmap's one-line description for this phase is "Failure classification,
bounded backoff for transient classes, per-angle retry endpoint, `REJECTED`
handling." Three of those four are already fully built, not by this phase:

- **Failure classification (§4)** — every `ProviderError` raised by
  `GeminiProvider` already carries a `failure_class`;
  `app/services/generation_service.py::transform_photo` already maps it onto
  the sub-job (Phase 6).
- **Bounded in-process backoff for transient classes** — `transform_photo`'s
  own `while sub_job.attempt_count < MAX_ATTEMPTS` loop already retries
  `RATE_LIMITED` / `TRANSIENT_PROVIDER` / `TRANSIENT_NETWORK` up to 3 times
  before giving up (Phase 6, documented as a deliberate in-process
  simplification vs literal Celery backoff — see that phase file).
- **`REJECTED` handling** — `_fail()` already routes `SAFETY_REFUSAL` to
  `SubJobStatus.REJECTED` instead of `FAILED` (Phase 6/7).

Nothing in this phase touches any of the above. Re-verified reading the code,
not re-built.

**What's actually missing** is the one piece every prior phase note has
pointed at: `POST /jobs/{job_id}/angles/{angle}/retry`
(`app/api/v2/retry.py`) is still `MOCK_MODE`-gated and mutates nothing —
`if not settings.MOCK_MODE: raise NotImplementedError(...)`.
`app/services/job_service.py::check_retry_preconditions` already implements
every §5 precondition (`FAILED`-only, ceiling, unexpired input) and is
already wired into the route; what's missing is the real state mutation and
dispatch behind it. This phase's actual scope is narrower than its
roadmap-line title suggests — one endpoint, plus three real gaps found while
building it:

1. **Idempotency gap.** `docs/business-rules.md` §8 requires
   `Idempotency-Key` on `/retry`, same as `/generate`. `/generate`'s dedup is
   durable — a Postgres uniqueness constraint on `(client_id,
   idempotency_key)` plus `jobs.payload_hash` (Phase 2, migration `0003`,
   added specifically because "a Redis-only hash doesn't survive the 24h
   TTL"). A retry has no natural row to persist a durable marker on — it
   mutates an existing `sub_job`, it doesn't create one.
   `app/core/idempotency.py` already has `get_replay`/`store_replay`
   functions built in Phase 1 for exactly this shape of problem, unused since
   Phase 2 replaced them with the durable Postgres check for `/generate`. Reusing
   that exact `idem:{client_id}:{key}` Redis prefix for `/retry` would let a
   client that reuses one `Idempotency-Key` value across both endpoints
   silently clobber the other's stored marker. This phase adds a separate
   `retryidem:{client_id}:{key}` namespace instead — Redis-only, 24h TTL,
   **not as durable as `/generate`'s**. Accepted, not hidden: a replay outside
   the TTL window is indistinguishable from a fresh retry request and will
   execute as one. Given the 3-attempt ceiling this can cause at most one
   extra generation call ever, on one angle, per job — not the double-billing
   risk `/generate`'s idempotency exists to prevent for a whole job.
2. **`retryable` doesn't check the ceiling.** `app/services/status_service.py`
   computes `retryable = status == FAILED and failure_class in
   _RETRYABLE_FAILURE_CLASSES` — it never looks at `attempt_count`. A sub-job
   that has already exhausted `MAX_RETRY_ATTEMPTS` still reports
   `retryable: true` with a working-looking `retry_url` that then 409s the
   moment the ERP calls it. Same data contract the endpoint enforces;
   fixed alongside it in Step 1 so the two don't disagree.
3. **`attempt_count` double-increment risk.** `docs/schema.md` says
   `attempt_count` "increments on manual retry and internal backoff" — one
   shared column, not two counters. `transform_photo`'s loop already
   increments it once per provider attempt regardless of what dispatched the
   call. The retry endpoint must **not** increment it again itself, or a
   single client retry would silently cost two ceiling slots instead of one.
   Verified by reading `transform_photo` before writing Step 3, not assumed.

---

## Step 1 — `retryable` respects the ceiling

### What to do

`app/services/status_service.py::build_angle_status`: import
`MAX_RETRY_ATTEMPTS` from `app/services/job_service.py` and add
`sub_job.attempt_count < MAX_RETRY_ATTEMPTS` to the `retryable` condition
(alongside the existing `status == FAILED` and `failure_class` checks).

### Checkpoint 1

- [ ] A `FAILED` sub-job with a retryable `failure_class` but
      `attempt_count >= MAX_RETRY_ATTEMPTS` reports `retryable: false`,
      `retry_url: null`
- [ ] Existing retryable-angle behavior (`attempt_count < MAX_RETRY_ATTEMPTS`)
      unchanged — `test_partial_success_failed_angle_is_retryable_with_url`
      still passes

---

## Step 2 — Retry-scoped idempotency

### What to do

`app/core/idempotency.py`: add `get_retry_target(client_id, idempotency_key)
-> str | None` and `store_retry_target(client_id, idempotency_key, target:
str) -> None`, keyed `retryidem:{client_id}:{idempotency_key}` (distinct
prefix from `/generate`'s `idem:`), same `TTL_SECONDS`. `target` is
`f"{sub_job.id}"` — enough to detect both a same-request replay (target
matches) and a same-key-different-target conflict (target differs).

### Checkpoint 2

- [ ] Same `Idempotency-Key` + same job/angle twice → second call is a no-op
      replay, `202`, no second `RETRY_REQUESTED` event, no second dispatch
- [ ] Same `Idempotency-Key` against a *different* job or angle → `409`
      `IDEMPOTENCY_KEY_CONFLICT`

---

## Step 3 — Real retry execution

### What to do

New `app/services/retry_service.py::execute_retry(session, job, sub_job) ->
None` (kept separate from `job_service.py`'s precondition check, mirroring
the existing `orchestration_service.py` split — one small, focused, directly
testable function):

1. Set `sub_job.status = PENDING`, clear `failure_class` and `error_message`.
2. If `job.status` is terminal (`status_rollup.TERMINAL_STATUSES`), set
   `job.status = PROCESSING` and `job.completed_at = None` immediately —
   mirrors `orchestration_service.dispatch_job`'s own eager `PROCESSING`
   write, so a `GET /status` in the gap between accept and the retried
   angle's actual completion doesn't show a stale terminal parent status
   next to a `PENDING` sub-job. The authoritative recompute still happens
   inside `transform_photo` once the retry actually finishes
   (`docs/business-rules.md` §2 — `compute_parent_status` already returns
   `PROCESSING` whenever `S + F < R`, true the instant one sub-job leaves a
   terminal state).
3. Record a `RETRY_REQUESTED` `JobEvent` (`from_status`/`to_status` on the
   sub-job; `detail` includes `angle` and the current `attempt_count` —
   **not incremented here**, see the reality check above).
4. Does not commit, does not dispatch — caller controls both.

`app/api/v2/retry.py`: remove the `MOCK_MODE` gate entirely (matches
`/generate`'s Phase 2 note — real regardless of the flag). New order:
resolve `job`/`sub_job` (unchanged 404s) → idempotency check (Step 2, replay
short-circuits before any precondition check or mutation) →
`check_retry_preconditions` (unchanged, still first real error path) →
`retry_service.execute_retry` → `idempotency.store_retry_target` → commit →
`generation.transform_photo_task.delay(str(sub_job.id))` — the exact
dispatch primitive Phase 7 built, reused per that phase's own closing note,
not reinvented.

### Checkpoint 3

- [ ] A `FAILED` sub-job with a retryable `failure_class`, retried → `202`,
      resets to `PENDING` then (via the real dispatched task, fixture-driven
      Gemini success) runs to a terminal state again;
      `attempt_count` reflects the additional attempt(s), not reset to zero
- [ ] Parent job moves from a terminal status back to `PROCESSING`
      immediately on retry accept (read mid-flight, before the dispatched
      task's own recompute lands)
- [ ] `REJECTED` sub-job → retry → `409 SUBJOB_NOT_RETRYABLE`, no mutation
      (existing `check_retry_preconditions` behavior, now exercised past the
      point where it used to dead-end at `MOCK_MODE`)
- [ ] Sub-job at the retry ceiling → `409 RETRY_LIMIT_EXCEEDED`
- [ ] `COMPLETED` sub-job → `409 SUBJOB_NOT_RETRYABLE`
- [ ] Expired input asset on a `FAILED` `UPLOADED` sub-job → `409
      INPUT_ASSET_EXPIRED`
- [ ] Retried angle reuses the job's pinned `config_version_id` — true by
      construction (`transform_photo` reads `job.config_version_id`, which
      this phase never touches), confirmed by the retried angle's
      `prompt_snapshot`/`model_version` matching the pinned version, not
      whatever the currently-active config version is
- [ ] A synthetic angle's retry still lands in `QA_REVIEW`, not `COMPLETED`,
      on success — same `transform_photo` code path, no special-casing
      needed or added
- [ ] `RETRY_REQUESTED` `JobEvent` row exists with correct `from_status` /
      `to_status` after a successful retry accept

---

## Step 4 — Self-audit

Same discipline as every prior phase: re-read every checkpoint above with
real tests (testcontainers Postgres, fakeredis or real local Redis, real
Supabase Storage, fixture-driven Gemini — same stack Phase 7 used), fix
failures before declaring done. Sync `docs/api-routes.md` (note the
`retryidem:` idempotency caveat), `docs/business-rules.md` §5 if anything
here diverged from it, `CLAUDE.md`, and `phases/phase-roadmap.md`. Delete or
rewrite `test_mock_mode_false_makes_retry_raise` — the behavior it asserts
(`MOCK_MODE=False` makes `/retry` 500) no longer exists once the gate is
removed; replace it with real-path coverage instead of leaving a test that
asserts removed behavior.

---

## Note for Phase 9

Phase 9 (Output QA Gate) is the first phase to actually score
`QA_REVIEW` sub-jobs and move them to `COMPLETED`/`REJECTED`. It should reuse
`status_rollup.compute_parent_status` the same way this phase and Phase 7
did — nothing about QA decisions needs a new rollup path. A QA-rejected
sub-job (`failure_class: QA_REJECTED`) is `REJECTED`, not `FAILED`, so
`check_retry_preconditions` already refuses to retry it without any change
needed here.
