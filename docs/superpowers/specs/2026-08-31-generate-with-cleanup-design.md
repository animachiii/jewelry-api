# GENERATE_WITH_CLEANUP — one call, two-phase pipeline

**Status:** design approved 2026-08-31, not yet implemented.

A client uploads one photograph. The backend cleans its background, then
generates 1-4 catalogue angles **from that cleaned image** rather than from the
raw upload. One API call, one `job_id`, one poll loop.

This is the seventh operation family. It is the first whose sub-jobs are not
all created at request time, and the first whose job carries both angled and
angle-less sub-jobs.

---

## 1. Why this exists

Client photographs arrive cluttered — mannequin, price tag, a hand, a display
case behind the piece. Mode A angle generation already performs background
removal inside each angle's own Gemini call, but it does so independently per
angle, from the same cluttered original, with no shared notion of what "clean"
looked like. Cleaning once and generating every angle from that single clean
base is the client's request.

**Decisions taken with the user before design (all four settled):**

| Question | Decision |
| :--- | :--- |
| Relationship between the steps | **Sequential.** Cleanup's output is the source photo every angle is generated from. |
| API shape | **New route + new `operation_t` value**, matching how MATCH/RECOLOR/MIX were each added rather than overloading `/generate`. |
| Is the cleaned photo a client deliverable? | **No — internal only.** It exists to feed the angle stage. The response carries angles only. |
| Does the cleanup step keep background removal's mandatory QA gate? | **No.** It is an internal preprocessing stage, not a standalone product being sold — the same posture Mode A real-photo angles already have (`docs/business-rules.md` §7). |

The QA decision is what makes this pipeline viable at all. A standalone
`BACKGROUND_REMOVAL` sub-job **always** enters `QA_REVIEW` (§13, unconditional).
Had the cleanup step kept that gate, a client's single API call would block on an
ops person clicking approve in the review queue before any angle could generate.

---

## 2. What the client sends and gets

**`POST /api/v2/generate-with-cleanup`** — `client` scope, `Idempotency-Key`
required, same durable `(client_id, key)` + `payload_hash` dedup as `/generate`.

```json
{
  "storage_path": "pending/{client_id}/{group}/CLEANUP/photo_abc.jpg",
  "category_code": "RING",
  "angles": ["FRONT", "SIDE", "DIAGONAL"],
  "sku_reference": "RING-0142",
  "metadata": {}
}
```

`angles` is a plain list of angle codes — **not** `/generate`'s per-angle object.
Every angle derives from the one cleaned photo, so there is no per-angle
`storage_path` to supply and nothing to choose per angle.

**Deliberately not supported in v1:** mixing `synthetic: true` angles into the
same request. A client wanting synthetic angles already has `/generate`.
Supporting both modes here would mean this route reproduces `/generate`'s full
per-angle union type for one hypothetical caller — YAGNI.

**Presign** gains `{"operation": "GENERATE_WITH_CLEANUP"}`, returning a single
`operation_upload` slot, the same shape MATCH already uses.

**Validation order** (all `4xx` before any row is created):

1. `operations.GENERATE_WITH_CLEANUP.enabled` — `422 OPERATION_DISABLED`
2. `category_code` exists and is active — `422 CATEGORY_NOT_FOUND` / `CATEGORY_INACTIVE`
3. Every requested angle is `enabled` for that category — `422 ANGLE_NOT_ENABLED`
4. At least one angle requested — `422 VALIDATION_ERROR`
5. `storage_path` exists, belongs to this client, passes
   `image_validation.inspect_and_validate` — `422 ASSET_NOT_FOUND` /
   `ASSET_NOT_OWNED` / `VALIDATION_ERROR`

`synthetic_allowed` does not apply — nothing here is synthetic.

**`GET /status/{job_id}`** returns `operation: "GENERATE_WITH_CLEANUP"` and
populates `angles`, the existing per-angle shape. `results` and `variants` stay
empty. The cleanup sub-job is **never** exposed — see §6.

---

## 3. Data model

**New `operation_t` value** `GENERATE_WITH_CLEANUP` via `ALTER TYPE ... ADD
VALUE`, the mechanism migrations `0013`/`0015`/`0017` already established.

**New column `jobs.requested_angle_codes`** (`JSONB`, nullable). The worker needs
to know which angles to create *after* cleanup finishes, and by then the request
body is long gone. This follows `jobs.preset_code`'s precedent exactly — Phase 15
added that column for the identical reason, having first tried to leave the value
only in the `JOB_CREATED` audit event and found the worker could not read it.
Reading business state out of the audit log is not a pattern this codebase
allows.

`jobs.requested_angles` keeps its existing meaning: the count of angles
requested. It is metadata, not rollup input — see §5.

**No new `asset_kind_t` value.** The cleanup output is a genuine provider
response and is stored `OUTPUT`-kind in `BUCKET_OUTPUTS`, exactly as
`background_service` already stores its own. That it is consumed internally
rather than returned does not change what it is. It inherits `OUTPUT`'s 180-day
retention.

**No new index.** A job carrying one angle-less cleanup sub-job plus N angled
sub-jobs is already legal: `ux_sub_jobs_job_single` covers `(job_id) WHERE angle
IS NULL AND variant_index IS NULL` (one cleanup row), `ux_sub_jobs_job_angle`
covers `(job_id, angle) WHERE angle IS NOT NULL` (the angle rows). Verified
against migration `0013`'s definitions.

**`validate_operation_angle_consistency` must change.** It currently enforces
"angle non-null **iff** operation is `ANGLE_GENERATION`", which this operation
violates in both directions — it has an angle-less sub-job (tripping the second
branch) and angled sub-jobs (tripping the first). This is the first operation
with heterogeneous sub-job shapes, so the invariant genuinely needs a third
case: for `GENERATE_WITH_CLEANUP`, either shape is valid.

---

## 4. Orchestration — two phases

```
POST /generate-with-cleanup
  └─ one transaction: Job + ONE cleanup sub-job (angle NULL) + INPUT asset
       └─ dispatch cleanup.process

cleanup.process  (worker wrapper)
  └─ cleanup_service.process()            ← Gemini call, no QA gate
       ├─ FAILED/REJECTED → recompute parent → job FAILED. Done.
       └─ COMPLETED, output asset written
  └─ after commit, in the WORKER layer:
       ├─ create N angle sub-jobs, input_asset_id = cleanup's output
       └─ dispatch generation.transform_photo per angle   ← unmodified
```

**Angle sub-jobs are created only after cleanup succeeds.** This is the single
most important structural decision, and it reverses my own first draft. Three
independent problems forced it:

1. **The reconciliation sweep would fail them.**
   `reconciliation_service.STUCK_STATUSES = (PENDING, GENERATING)` selects every
   sub-job in those states across all jobs and fails any whose last activity
   exceeds `RECONCILIATION_STALE_AFTER_SECONDS` (600s). Pre-created, undispatched
   angle rows sit at `PENDING` for the whole cleanup phase — which can plausibly
   exceed 600s (three provider attempts at up to `WORKER_TASK_TIMEOUT_SECONDS`
   = 180s each, plus queue wait). This would fail healthy jobs intermittently in
   production. Excluding them from the sweep instead would mean special-casing a
   safety net that exists precisely to catch never-dispatched work.
2. **Cascading a cleanup failure onto pre-created rows needs an illegal
   transition.** Marking them `REJECTED` then reviving them to `PENDING` on a
   successful cleanup retry is a sub-job transition §2 does not permit.
3. **All-or-nothing job retry would deadlock.** §14 validates every failed
   sub-job's preconditions before executing any. Angle rows with a null
   `input_asset_id` fail `check_retry_preconditions`
   (`InputAssetExpiredError`), so the whole retry request would `409` — leaving
   the job permanently unretryable.

Deferring creation dissolves all three: the rows do not exist to be swept,
cascaded onto, or wrongly retried.

**Dispatch happens in the worker wrapper, never inside the service.** This
mirrors the rule Phase 9 established for `qa.score_similarity` — dispatching from
inside a service risks the next task reading rows before the creating transaction
lands.

**`cleanup_service` is a new module, not a flag on `background_service`.** The
QA-gated path must stay exactly as it is for standalone `/background/remove`.
The two share the provider call and prompt-resolution shape; they differ in
whether success routes to `QA_REVIEW` or straight to `COMPLETED`. Following this
codebase's established preference (`recolor_service`/`mix_service`/`match_service`
each reimplement rather than import each other's private helpers), this is a
separate module.

---

## 5. Status, failure, and cost

**Parent rollup needs no changes at all.** `recompute_parent_status` counts
actual sub-job rows — verified, it does not read `jobs.requested_angles`. So:

| Moment | Rows | R | S | F | Parent |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Cleanup running | 1 cleanup | 1 | 0 | 0 | `PROCESSING` |
| Cleanup failed | 1 cleanup | 1 | 0 | 1 | `FAILED` — correct, nothing more is coming |
| Angles dispatched | 1 + N | N+1 | 1 | 0 | `PROCESSING` |
| All angles succeed | 1 + N | N+1 | N+1 | 0 | `COMPLETED` |
| Some angles fail | 1 + N | N+1 | <N+1 | >0 | `PARTIAL_SUCCESS` |

The cleanup sub-job simply counts as one more unit. `jobs.requested_angles`
(= N) disagreeing with the row count (= N+1) is harmless: it is reporting
metadata, not rollup input.

**A cleanup failure fails the job outright**, with no special-casing — because
the only existing row is the failed one, `F = R` falls out of the unmodified
rollup. `failure_class` and `error_message` land on the cleanup sub-job, which
ops can read via `GET /jobs` and `job_events`.

**Phase-1 response shape.** During cleanup no angle rows exist, so `angles` would
be `[]` — visibly different from `/generate`, which shows every angle at
`PENDING` immediately. `status_service` therefore synthesizes the pending list
from `requested_angle_codes` when no angle sub-jobs exist yet, so clients see the
same shape either way. This is a display concern solved at the display layer; it
creates no phantom rows for the sweep to find.

**Cost is correct with zero special-casing.** Every provider call already writes
its own `cost_events` row and a job's cost is their sum (§10). A
`GENERATE_WITH_CLEANUP` job bills 1 cleanup call + N angle calls + any retries.
`unit_cost_usd` resolves per-operation with the standard fallback to
`config.global.unit_cost_usd`.

---

## 6. What is deliberately not exposed

The cleanup sub-job appears in **no** client-facing response array — not
`angles`, not `results`. The client chose "internal only"; surfacing it as a
result item with a suppressed `image_url` would contradict that and invite
questions about a step they cannot act on.

It remains fully visible where it needs to be: ops sees it in `GET /jobs`, its
cost in `GET /jobs/{job_id}/cost`, and its full transition history in
`job_events`. Nothing about the audit trail is weakened.

**Diagnosability of a cleanup failure:** the job reports `FAILED` and the
client's own `angles` array is empty. The failure reason lives on the cleanup
sub-job, which ops can read. If this proves too opaque for the Flutter team in
practice, the fix is a job-level `error_message` — not exposing the sub-job.
Flagged as a known, accepted limitation rather than solved speculatively.

---

## 7. Retry

**An angle fails, cleanup already succeeded** — the existing per-angle route
`POST /jobs/{job_id}/angles/{angle}/retry` works completely unmodified. The
sub-job's `input_asset_id` already points at the cleanup output, so a retry
re-runs `generation.transform_photo` against the same clean base. Nothing to
build.

**Cleanup fails** — only one sub-job exists and it is `FAILED`, so
`POST /jobs/{job_id}/retry` retries exactly it under §14's existing
all-or-nothing logic (a set of size one is trivially all-or-nothing, the same
reasoning RECOLOR and MIX already rely on). On success it creates and dispatches
the angle sub-jobs through the identical code path as the original run — no
revival, no illegal transition, no new state-machine rule.

The route needs **one** change: a new entry in its `job.operation` →
dispatch-task lookup, so a retried cleanup sub-job dispatches `cleanup.process`.
That is the same one-line-per-operation extension MATCH, RECOLOR and MIX each
added.

---

## 8. Testing

**Unit**
- `validate_operation_angle_consistency` accepts both shapes for this operation and still rejects both violations for every other operation
- Angle-list validation: unknown angle, angle disabled for the category, empty list

**Integration** (testcontainers Postgres, real local Redis, real Supabase Storage, fixture-driven Gemini — this repo never mocks Storage and never calls live Gemini in CI)
- Happy path: 3 angles requested → 4 sub-jobs exist, all `COMPLETED`, job `COMPLETED`, every angle's `input_asset_id` is the cleanup output — **not** the client's upload. This is the test that proves the pipeline actually chains.
- Cleanup fails → job `FAILED`, exactly one sub-job exists, zero angle sub-jobs were created
- Cleanup succeeds, one angle fails → job `PARTIAL_SUCCESS`, that angle is retryable
- Cleanup retry after failure → angle sub-jobs are created and dispatched on the retry
- Cleanup sub-job never appears in `GET /status`'s `angles`/`results`/`variants`
- Phase-1 poll shows synthesized `PENDING` angles, not `[]`
- Cost report includes both the cleanup call and every angle call
- Presign returns one slot for this operation
- Idempotent replay returns the original `job_id`; same key + different body `409`s

**Explicitly not covered, and cannot be from CI:** whether cleaning first
actually produces better angle renders than Mode A's per-angle background
removal already does. That is the product question this feature rests on, and it
needs real client pieces through a real Gemini call — the same gap every phase
since 6 has carried.

---

## 9. Open items

- **Naming.** `GENERATE_WITH_CLEANUP` / `/generate-with-cleanup` is a
  placeholder the user may want to change; it touches the enum, route, presign
  value and config key, so it is cheapest to settle before implementation.
- **Prompt and price are uncalibrated**, same status as every other seeded
  operation. The cleanup step needs its own prompt in
  `config.global.operations.GENERATE_WITH_CLEANUP`; whether it should reuse
  `BACKGROUND_REMOVAL`'s existing wording verbatim or diverge (it feeds a
  generator rather than a human buyer) is a real question with no data behind it
  yet.
- **Whether cleanup should be skippable** when a client's photo is already clean
  — deferred; there is no signal to decide it on and it doubles the flow's state
  space.
