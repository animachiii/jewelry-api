# Phase 15 — Standalone Background Operations

## Objective

Add two client-invocable operations that are **not** tied to the four-angle
generation flow:

| Operation | What the client gets |
| :--- | :--- |
| `BACKGROUND_REMOVAL` | The product isolated from its original background |
| `BACKGROUND_REPLACEMENT` | The product on a chosen backdrop from a curated preset list |

Both take **one** uploaded photograph and return **one** image. They reuse the
existing job/sub-job state machine, status polling, retry, idempotency, cost
recording, and audit trail rather than growing a parallel pipeline beside them.

This phase does **not** revive local matting. See Step 1 for why that door is
closed on current infrastructure, and what the real options are.

---

## Context — what is actually true right now

Read this before designing anything. Several of these were learned the
expensive way on 2026-08-12 and contradict what the older docs imply.

**The pipeline today.** `POST /generate` → `orchestration.fan_out_job` → one
`generation.transform_photo` per angle → `GeminiProvider.generate()` → upload to
`jewelry-outputs` → `GET /status/{job_id}` polls. One Gemini call does
background removal, background synthesis, relighting, and shadow *together*
(`docs/decisions/0001-drop-local-matting.md`).

**There is no matting infrastructure and no GPU.** No `gpu` queue, no
`jewelry-mattes` bucket, no `MATTING_MODEL_ID`. `sub_job_status_t.MATTING` and
`asset_kind_t.MATTE` survive as dead Postgres enum values only. Do not use them.

**Production runs on a 512MB Render free instance.** `IO_QUEUE_CONCURRENCY=1`
with Celery prefork — beat + worker main + **one** worker child + uvicorn share
that 512MB. Anything above ~4 forked children OOM-kills the whole container
(`render_start.sh`'s `wait -n` tears down all three processes together).
`--max-tasks-per-child=20` recycles the child so lazily-imported SDKs don't
accumulate. **Every operation in this phase is serialised through that single
child**, behind whatever angle jobs are already queued.

**Known landmines in the generation path — do not reintroduce these:**

- Decode SDK image bytes with `gemini.py::_decode_b64_any_alphabet`, never bare
  `base64.b64decode`. `model_dump(mode="json")` emits **URL-safe** base64 and
  `b64decode` silently mis-decodes it into a right-sized, entirely corrupt file.
- `parts[0]` is **not** the image. Gemini 3.x image models are thinking models:
  part 0 is text narration and up to two interim draft images can precede the
  real one. Take the *last* part with real `inline_data`.
- Celery task bodies must use `new_redis_client()` (owned, `aclose()`d), never
  the `get_redis_client()` singleton — `run_async` calls `asyncio.run()`, which
  closes the loop and leaves a cached client bound to a dead one.
- Worker regression tests must be plain `def`, not `async def`, or `run_async`
  takes the background-loop branch and hides exactly these bugs.

**The Google Sheet has no Global tab.** `config_sync_service.normalize_sheet_rows`
inherits the whole `global` block from the previously-active version. **A new
config key therefore cannot arrive from the Sheet.** It has to be seeded by an
Alembic data migration and inherited forward — the pattern migration `0005`
already established for `model_version`.

**Real-photo angles have no QA gate.** Mode A output is returned unchecked
(`docs/ai-integration.md` Call Site 1, accepted risk). That gap matters more
here: for these operations the cutout *is* the product, so Step 5 adds a gate
rather than inheriting the omission.

---

## Step 1 — Decide how "remove background" actually produces its output

**This is the blocking decision. No other step can be specified until it lands.**
Do not start Step 2 before this is written up.

### The constraint, already established

Background removal is only commercially useful if the output composites onto
other backgrounds — which requires an **alpha channel**. The current integration
cannot produce one:

- `types.ImageConfig.output_mime_type` and `.image_output_options` are both
  documented **"This field is not supported in Gemini API"** — they are Vertex-only.
  Verified against the installed `google-genai` in this repo.
- The output format is therefore the model's choice, and every production output
  so far has been `image/jpeg`. **JPEG has no alpha channel.**

So "transparent PNG straight out of `GeminiProvider`" is not currently on the
table, and any plan assuming it is will fail late.

### What to do

Run a timeboxed spike (half a day, no production code committed) that answers
one question: **what is the best achievable output, and at what cost?**

Evaluate these four, on **at least 12 real client pieces** that must include ≥3
fine chains, ≥2 transparent/translucent stones, and ≥2 high-polish metal
surfaces — the cases that break cutouts:

| Option | Alpha? | Infra cost | Notes |
| :--- | :--- | :--- | :--- |
| **A. Gemini, flat solid background** | No | None — current stack | Reframes the feature as "background standardisation". Honest and free. |
| **B. Hosted matting API** (Bria RMBG API, Clipdrop, remove.bg) | Yes | Network-bound, fits the `io` queue, no GPU | Per-image fee. Bria's *hosted API* also sidesteps the CC BY-NC problem that killed the self-hosted model. |
| **C. Vertex AI instead of Gemini API** | Maybe | New auth (service account), new client, region config | Only worth it if the spike proves Vertex actually honours `output_mime_type: image/png` **with** real alpha. Verify — do not assume. |
| **D. Local BiRefNet** | Yes | **Blocked** | Needs GPU + far more than 512MB. Not viable without leaving Render's free tier. Listed only so it is explicitly ruled out. |

For each option record, per image: whether fine chain detail survived, whether
transparency was handled or flattened, output format and whether alpha is
genuinely present *and varying* (not a fully-opaque alpha channel), latency, and
per-image cost.

### Checkpoint 1

- [ ] Spike run over ≥12 real pieces meeting the category requirements above
- [ ] For every candidate that claims alpha: output verified as PNG colour type 4
      or 6 **and** the alpha channel proven to contain more than one distinct
      value — a fully-opaque alpha channel is a fake pass
- [ ] Per-image latency and per-image cost recorded for each option
- [ ] `docs/decisions/0002-background-removal-approach.md` written: the choice,
      the evidence, the per-image cost, and the rejected options with reasons
- [ ] If the chosen option is **B** or **C**: the new dependency's credentials
      are added to `app/config.py::Settings` **and** to `docs/deployment.md`'s
      secrets table — `tests/unit/test_deployment_docs.py` fails otherwise
- [ ] If the chosen option is **A**: the client has confirmed in writing that a
      flat solid background (no transparency) meets the need, **before** any
      code is written. Do not build A and discover this at handover.

---

## Step 2 — Schema: make a job's operation explicit

### What to do

The existing schema hardcodes the assumption that a sub-job is an angle:
`sub_jobs.angle` is `NOT NULL` and unique per `(job_id, angle)`. These
operations have no angle.

Write migration `0006_add_job_operation`:

```
CREATE TYPE operation_t AS ENUM (
  'ANGLE_GENERATION', 'BACKGROUND_REMOVAL', 'BACKGROUND_REPLACEMENT'
);

ALTER TABLE jobs
  ADD COLUMN operation operation_t NOT NULL DEFAULT 'ANGLE_GENERATION';

ALTER TABLE sub_jobs ALTER COLUMN angle DROP NOT NULL;
ALTER TABLE sub_jobs DROP CONSTRAINT <the (job_id, angle) unique constraint>;
CREATE UNIQUE INDEX ux_sub_jobs_job_angle
  ON sub_jobs (job_id, angle) WHERE angle IS NOT NULL;
CREATE UNIQUE INDEX ux_sub_jobs_job_single
  ON sub_jobs (job_id)        WHERE angle IS NULL;
```

The `DEFAULT 'ANGLE_GENERATION'` is what makes this safe against live data —
every existing row is an angle job and stays one. **Keep the default** rather
than backfilling and dropping it; a new angle job should not have to state the
obvious.

The second partial index enforces "a background job has exactly one sub-job" in
the database, not in a service method.

A cross-table CHECK (angle non-null *iff* the parent job is `ANGLE_GENERATION`)
is not expressible in Postgres. Enforce it in `job_service` and pin it with a
test — do not denormalise `operation` onto `sub_jobs` just to get the CHECK; two
sources of truth for the same fact is the worse trade.

**`compute_parent_status` needs no change.** It counts requested/succeeded/failed
and is angle-agnostic; a one-sub-job background job rolls up to `COMPLETED` or
`FAILED` through the existing rules (`R=1`, so `PARTIAL_SUCCESS` is unreachable —
which is correct). `jobs.requested_angles` keeps its name and holds `1`; renaming
a live column across the Flutter contract is not worth the cosmetics.

### Checkpoint 2

- [ ] `alembic upgrade head` applies against a database holding real angle jobs;
      every pre-existing row reads `operation = 'ANGLE_GENERATION'`
- [ ] `alembic downgrade` then `upgrade` round-trips cleanly (test as in
      `tests/integration/test_migrations.py`, plain `def`, real testcontainers)
- [ ] Two sub-jobs with the same `(job_id, angle)` still raise a unique violation
- [ ] A second `angle IS NULL` sub-job on the same job raises a unique violation
- [ ] An angle sub-job on a `BACKGROUND_REMOVAL` job is rejected by
      `job_service`, with a test proving it
- [ ] `alembic check` reports no ORM/migration drift
- [ ] `compute_parent_status` is **unmodified**, and a test asserts a 1-sub-job
      failed job is `FAILED`, never `PARTIAL_SUCCESS`

---

## Step 3 — Config: presets, not free-text prompts

### What to do

Background replacement needs a target backdrop. Take it from a **curated preset
list in the config version**, not from client-supplied free text.

Free text would put an unvalidated string into a generation prompt: unbounded
quality variance, a new safety-refusal surface, and no way to reproduce what a
given job actually rendered. Presets keep this in the same versioned,
hash-tracked, job-pinned place every other business rule lives
(`docs/conventions.md`: "Business configuration lives in the config version").

Extend `config_versions.payload`:

```json
{
  "operations": {
    "BACKGROUND_REMOVAL":    { "enabled": true, "prompt": "...", "unit_cost_usd": 0.02 },
    "BACKGROUND_REPLACEMENT":{ "enabled": true, "unit_cost_usd": 0.02 }
  },
  "background_presets": [
    { "code": "STUDIO_WHITE", "name": "Studio White", "prompt": "...",
      "reference_image_urls": [], "is_active": true }
  ]
}
```

**The Sheet cannot supply these** (no Global tab — see Context). Seed them with
an Alembic data migration that copies the active payload, adds the new keys, and
activates a new version — exactly what `0005_fix_gemini_model_version` does.
Never mutate a `config_versions` row (Hard Rule 11).

`normalize_sheet_rows` must carry both new keys through the inherited `global`
block untouched, or the next config sync will silently drop them. This is a real
regression risk: add a test for it.

Expose presets on `GET /config` (`code` + `name` only — prompts stay internal,
same rule angle prompts already follow) so the ERP can render a picker.

### Checkpoint 3

- [ ] Data migration seeds `operations` and `background_presets` into a **new**
      active config version; the previous row is untouched and deactivated
- [ ] A config sync after the migration **preserves** both keys — test with a
      recorded sheet fixture, asserting they survive `normalize_sheet_rows`
- [ ] `GET /config` returns preset `code` and `name`, and **no** `prompt` — the
      existing secret-leakage test extended to cover preset prompts
- [ ] Requesting a `preset_code` that is absent or `is_active: false` returns
      `422` with a specific error code, not a 500
- [ ] Requesting an operation whose `operations.<OP>.enabled` is `false` returns
      `422`
- [ ] `unit_cost_usd` resolves per-operation, falling back to `global.unit_cost_usd`
      when absent — a job must never bill at a hardcoded rate

---

## Step 4 — API: two endpoints, one existing status route

### What to do

Add two routes. Both `client` scope, both require `Idempotency-Key`, both return
the existing `JobAcceptedResponse` with `202`:

```
POST /api/v2/background/remove   { storage_path, sku_reference?, metadata? }
POST /api/v2/background/replace  { storage_path, preset_code, sku_reference?, metadata? }
```

Two explicit endpoints rather than one `{operation: ...}` body: the request
shapes genuinely differ (`preset_code` is required for one and meaningless for
the other), and a discriminated body would push that into runtime validation
where the OpenAPI spec cannot express it.

Validation order mirrors `/generate` — every failure is `4xx` **before** any job
row exists: operation enabled → preset exists and is active (replace only) →
`storage_path` exists in `jewelry-inputs` and belongs to this client → image
passes `image_validation.inspect_and_validate`.

**Presign:** `/uploads/presign` builds paths as
`pending/{client_id}/{group_id}/{angle}/...`. Add an operation-aware form so
these ops can presign without inventing a fake angle; use the operation name as
that path segment.

**Status:** reuse `GET /status/{job_id}` unchanged as an endpoint. Add to the
response body:

- `operation` — so the client knows which shape to read
- `results` — a one-element array for background jobs, carrying the same
  per-item fields as `AngleStatus` minus `angle`

Keep `angles` exactly as it is for angle jobs. **Do not make `AngleStatus.angle`
nullable** — a strict Flutter client treats that as breaking, and
`docs/conventions.md` says a breaking change bumps the API version. Two additive
fields do not.

**Retry:** the existing route is angle-specific
(`/jobs/{job_id}/angles/{angle}/retry`). Add `POST /api/v2/jobs/{job_id}/retry`
for single-sub-job jobs, reusing `retry_service.execute_retry` and the same
`retryidem:` Redis namespace and preconditions (`FAILED` only, attempt ceiling,
input unexpired). Returns `409` on an angle job — that job type must keep naming
its angle.

### Checkpoint 4

- [ ] Both routes exist at the documented method and path with `client` scope,
      and 401 without a key
- [ ] Missing `Idempotency-Key` returns `IDEMPOTENCY_KEY_REQUIRED`
- [ ] A replayed key returns the original `202` and creates no second job and no
      second `cost_events` row
- [ ] The same key with a different payload returns `409`
- [ ] Another client's `storage_path` returns `404`, never `403`
- [ ] `GET /status/{job_id}` for a background job returns `operation`, a
      one-element `results`, and an **empty** `angles`
- [ ] `GET /status/{job_id}` for an existing angle job is **byte-identical** to
      before this phase apart from the additive `operation` field — assert
      against a committed pre-change response fixture
- [ ] `POST /jobs/{job_id}/retry` returns `409` on an angle job and `202` on a
      `FAILED` background job
- [ ] `docs/openapi.json` regenerated, committed, and CI's diff check passes

---

## Step 5 — Worker, provider, and the subject-preservation gate

### What to do

Add `background.process` in `app/workers/background.py` — session lifecycle
only, real logic in `app/services/background_service.py`, mirroring the
`generation.py` / `generation_service.py` split. **Copy the lifecycle from
`app/workers/generation.py` verbatim**: per-call engine from
`settings.DATABASE_URL` read live, per-call `new_redis_client()` closed in
`finally`, dispatch through `run_async`. Route it to the `io` queue.

Reuse without modification: `rate_limiter` (the Gemini budget is global — these
calls compete with angle jobs and must share the same window),
`cost_service.record`, `storage_service.upload_bytes`,
`status_rollup.compute_parent_status`, `retry_service.execute_retry`.

Extend `GenerationProvider` with the new operation, or add a sibling method —
whichever keeps `app/providers/` the only place importing the SDK. **Route all
image-byte decoding through `_decode_b64_any_alphabet` and the
last-`inline_data`-part rule.** A fresh `base64.b64decode` here re-creates the
exact corruption that shipped to production once already.

**The QA gate.** Unlike Mode A, these operations must not return unchecked
output: the subject is meant to be *identical* to the input, with only the
background changed, so a drifted or partly-eaten product is both detectable and
unacceptable. Reuse Phase 9's `qa_service` machinery with the **input image** as
the reference instead of the category reference matrix. Below threshold →
`QA_REVIEW` → the existing human queue. A QA provider failure fails open to a
human, never to an unscored pass — the rule `docs/ai-integration.md` already sets.

Calibrate this threshold separately and expect it **higher** than the synthetic
angles' `0.82`: "same object, new background" should score far closer to 1.0
than "novel view of the same object". Treat any inherited value as a placeholder
until scored against real pieces.

**Budget the queue cost honestly.** Generation + QA is **two** Gemini calls per
operation, serialised through the single worker child behind any angle jobs
already queued. At ~30–60s per call that is minutes of latency under trivial
load. If that is unacceptable, the fix is a paid instance with real concurrency,
not silently dropping the QA gate.

### Checkpoint 5

- [ ] `background.process` registered and routed to `io`; a test asserts the
      route, as `test_generation_task_registration.py` does
- [ ] The worker builds its own engine **and** its own Redis client per call, and
      closes both — proven by a **plain `def`** test calling the task twice in
      one process (the pattern in `test_worker_event_loop_lifecycle.py`; an
      `async def` version of this test cannot catch the bug)
- [ ] A response whose first part is text and whose second is the image decodes
      to the **image** — fixture built with the real SDK serializer, not
      hand-written base64
- [ ] Round trip: submit → poll → download the output → it opens as a valid
      image, asserted on magic bytes, not on the sub-job saying `COMPLETED`
- [ ] A below-threshold QA score lands the sub-job in `QA_REVIEW`, parent
      `PROCESSING`, and the item appears in `GET /qa/review-queue`
- [ ] A QA provider failure lands `QA_REVIEW` + `FLAGGED` with `qa_score: NULL`,
      never `COMPLETED`
- [ ] Exactly one `cost_events` row per provider call, including refusals, at the
      per-operation `unit_cost_usd`
- [ ] A safety refusal lands `REJECTED` with `retryable: false` and no retry URL
- [ ] Provider timeout is bounded by `GEMINI_REQUEST_TIMEOUT_SECONDS` and
      classifies as `TRANSIENT_NETWORK`, not an unbounded hang
- [ ] Measured end-to-end latency for one operation on the live free instance,
      recorded in `docs/capacity-tuning.md` — with the QA call included

---

## Step 6 — Docs and handoff

### What to do

A phase is not complete until `docs/` matches what was built.

- `docs/api-routes.md` — both routes, the retry route, the `operation` and
  `results` status fields, and every new error code
- `docs/schema.md` — `operation_t`, `jobs.operation`, nullable `sub_jobs.angle`,
  both partial indexes, and the new `payload` keys
- `docs/business-rules.md` — a new section: operation matrix, why
  `PARTIAL_SUCCESS` is unreachable for these jobs, the subject-preservation gate
  and its threshold
- `docs/ai-integration.md` — the new call site(s), including whatever Step 1
  chose, with its failure modes
- `docs/integration-guide.md` — the Flutter lifecycle for these operations, and
  explicitly: **read `operation` first, then `results` or `angles`**
- `CLAUDE.md` — update the Project Overview, which currently describes only the
  four-angle flow
- `phases/phase-roadmap.md` — mark this phase's status

### Checkpoint 6

- [ ] Every file above updated
- [ ] `tests/unit/test_deployment_docs.py` passes — every new `Settings` field
      appears in `docs/deployment.md`
- [ ] The error-code enum contains every new code and `docs/api-routes.md` lists
      them all
- [ ] `docs/openapi.json` committed and diff-checked in CI
- [ ] Flutter lead has confirmed in writing that the `operation` / `results`
      split is understood, and that a `QA_REVIEW` background job shows as still
      processing rather than as an error

---

## Self-Audit Instruction

Before declaring this phase complete:

1. Re-read every checkpoint in this file.
2. Test each one — run the command, query the database, download the image,
   inspect the bytes. **Do not mark a checkpoint from reading the code.** Two
   bugs this phase explicitly guards against (URL-safe base64, `parts[0]`)
   both passed code review and a green test suite before reaching production.
3. For every regression test added here, **revert the fix and confirm the test
   fails**, then restore. A test that has never failed has not been shown to
   test anything.
4. Report:

   ```
   ✅ [Checkpoint] — Pass
   ⚠️ [Checkpoint] — Partial: [specific reason]
   ❌ [Checkpoint] — Fail: [specific reason]
   ```

5. Fix all failures and partials before reporting complete.
6. Only say "Phase 15 Complete" when every box is green and `docs/` is in sync.

---

## Final Phase 15 Checklist

- [ ] Step 1 decision written up in `docs/decisions/0002-...` **with evidence**,
      and the client signed off if the answer is "no transparency"
- [ ] `operation_t` migrated; existing angle jobs provably unaffected
- [ ] Presets and per-operation config seeded, and proven to survive a sync
- [ ] Both endpoints live, idempotent, scope-enforced, spec committed
- [ ] Worker reuses the per-call engine + per-call Redis client pattern
- [ ] Subject-preservation QA gate live, threshold calibrated against real pieces
- [ ] Output verified by downloading real bytes, not by trusting `COMPLETED`
- [ ] Latency on the live free instance measured and recorded
- [ ] `docs/` and `CLAUDE.md` updated; self-audit green

---

## Open questions to resolve during this phase

1. **Transparency** — Step 1. Everything else depends on it. If the answer is
   "flat background only", confirm with the client **before** building.
2. **Preset list** — which backdrops does Sumangali actually want? Needs the
   same conversation as the angle prompt matrix, and the Sheet cannot carry them
   today.
3. **Pricing** — these are billable Gemini calls (two, with QA). Does the
   retainer cover them, or do they need their own per-image rate? Roadmap open
   decision #4 (volume) is still unresolved and blocks this too.
4. **Concurrency** — one worker child, two Gemini calls per operation. Does this
   need a paid Render instance before launch? Measure in Step 5, decide with a
   number rather than a guess.
5. **Batch** — is one-image-per-request enough, or does the ERP want to submit a
   whole SKU set? Batch is a different fan-out shape; do not design for it
   speculatively, but ask before freezing the contract.
