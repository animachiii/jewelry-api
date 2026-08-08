# Phase 6 — Gemini Generation Worker

## Reality check before writing this

Phases 0–4 are complete. `app/providers/base.py`, `app/providers/gemini.py`,
`app/workers/generation.py`, `app/workers/qa.py`, and
`app/services/cost_service.py` are all still `TODO` stubs from Phase 0 — this
phase is the first to touch any of them.

No real `GEMINI_API_KEY` exists in this environment (empty in `.env`, same
situation as the Sheets credentials Phase 3 hit) — `docs/ai-integration.md`
already anticipated this: "Never call the live Gemini API in CI. Use
recorded response fixtures in `tests/fixtures/gemini/`" (already populated:
`success.json`, `rate_limited_429.json`, `server_error_5xx.json`,
`safety_refusal.json`, `malformed_response.json`, `timeout.json`). This
phase follows the same isolation pattern Phase 3 used for Sheets
(`app/providers/sheets.py`): a thin, real SDK-calling function that nothing
but the provider imports, with a testable seam one level up that tests
substitute instead of touching the network.

**Scope boundary, from `phases/phase-roadmap.md`:** "`GenerationProvider`
abstraction, pinned model, token-bucket rate limiter, cost logging, refusal
handling. Real-photo angles now include background removal in this one call
(no matting step)." Split as `6a real-photo, 6b synthetic/reference-matrix`.
QA scoring is explicitly **Phase 9**, not this phase — `docs/ai-integration.md`
Call Site 2. So a Mode B (synthetic) generation that succeeds in this phase
has nowhere spec-compliant to land except `QA_REVIEW` (unscored,
`qa_score: NULL`) — the exact state `docs/business-rules.md` §7 already
defines for "awaiting a QA decision." Phase 9 adds the actual scoring;
`app/api/v2/qa.py`'s review-queue and decision endpoints (still
`NotImplementedError`) are what will eventually clear that queue. This phase
does not implement those — a synthetic angle generated now will sit in
`QA_REVIEW` until Phase 9 exists, which is correct, not a bug.

**Gap found and closed:** `docs/schema.md`'s `config_versions.payload.global`
shape (`model_version`, `qa_similarity_threshold`, `default_negative_prompt`)
has no cost field, but `docs/business-rules.md` §10 requires
`unit_cost_usd` to "come from configuration, never a hardcoded constant."
Adding `global.unit_cost_usd` to the payload shape (JSONB — no migration)
closes this, consistent with how Phase 2 and Phase 4 each found and closed
one contract gap while starting their work.

This phase does **not** build orchestration (Celery group fan-out, chord
rollup, parent-status recompute after a real transition — that's Phase 7)
or the retry endpoint's real execution (Phase 8). The task built here
transitions exactly one sub-job, called directly (`task_always_eager` in
tests, matching every other Celery task in this codebase so far).

---

## Step 1 — GenerationProvider abstraction + rate limiter

### What to do

`app/providers/base.py`: `GenerationResult` (frozen dataclass —
`image_bytes: bytes`, `mime_type: str`, `model_version: str`) and
`GenerationProvider` (ABC) with one method:
`generate(prompt: str, reference_images: list[bytes], seed: int) -> GenerationResult`.
Raises `ProviderError` (already in `app/core/errors.py`, carries
`failure_class`) on any failure — the provider classifies, callers don't.

`app/services/rate_limiter.py`: a Redis-backed fixed-window token bucket,
key `provider:gemini:tokens:{window}` (per-minute window, per
`docs/schema.md`'s `provider:gemini:tokens` key pattern), capacity from
`settings.GEMINI_RATE_LIMIT_PER_MINUTE`. `async def acquire(redis) -> bool`
— increments and returns whether the caller is under the limit this window.
Shared across every worker process, per `docs/ai-integration.md`: "Four
sub-tasks per job multiplied across concurrent jobs will otherwise burst
straight into 429s."

### Checkpoint 1

- [x] `GenerationProvider` is an ABC; `app/workers/generation.py` cannot
      import `google.genai` (grep-checkable) — only `app/providers/gemini.py`
      does, per `docs/conventions.md` ("`app/providers/` is the only place
      that imports a model SDK") — verified by
      `tests/integration/test_generation_worker.py::test_generation_task_never_imports_google_genai_at_module_level`
- [x] `rate_limiter.acquire` returns `True` for the first N calls in a
      window (N = configured limit) and `False` for the `N+1`th, tested
      against `fakeredis` — `tests/unit/test_rate_limiter.py`
- [x] The window resets on the next minute boundary (tested by manipulating
      the key/window directly, not by sleeping 60s in a test)

---

## Step 2 — GeminiProvider

### What to do

`app/providers/gemini.py`: `GeminiProvider(GenerationProvider)`. Internally
splits into `_call_api(...) -> dict` (the real `google.genai.Client` call,
never exercised in tests — same honesty as `providers/sheets.py`) and
`generate(...)` (calls `_call_api`, classifies the response). Response
classification, from `docs/ai-integration.md`'s failure table:

| Response shape | Outcome |
| :--- | :--- |
| `candidates[0].finish_reason == "STOP"` with inline image data | success — return `GenerationResult` |
| `finish_reason == "SAFETY"` or empty `candidates` | raise `ProviderError(failure_class=SAFETY_REFUSAL)` |
| HTTP 429 / `RESOURCE_EXHAUSTED` | raise `ProviderError(failure_class=RATE_LIMITED)` |
| HTTP 5xx | raise `ProviderError(failure_class=TRANSIENT_PROVIDER)` |
| Timeout / connection error | raise `ProviderError(failure_class=TRANSIENT_NETWORK)` |
| Anything else unparseable | raise `ProviderError(failure_class=INTERNAL)` |

Model version is never a floating alias — always the exact
`config.global.model_version` string passed in, recorded on the result.

### Checkpoint 2

All verified in `tests/unit/test_gemini_provider.py`:

- [x] Given `tests/fixtures/gemini/success.json`'s shape, `generate()` returns
      a `GenerationResult` with the decoded image bytes and the fixture's
      `model_version`. **Fixed the fixture itself** — `success.json`'s
      `data` field was the literal placeholder string
      `"<base64-image-bytes>"`, not valid base64, so success could never
      actually be tested before now. Replaced with a real tiny PNG's base64.
- [x] Given `rate_limited_429.json` → `ProviderError` with
      `failure_class == RATE_LIMITED`
- [x] Given `server_error_5xx.json` → `TRANSIENT_PROVIDER`
- [x] Given `safety_refusal.json` → `SAFETY_REFUSAL`
- [x] Given `malformed_response.json` → `INTERNAL`
- [x] A simulated timeout exception → `TRANSIENT_NETWORK`
- [x] None of these tests import or touch `google.genai`'s network layer —
      they call `generate()` with `_call_api` monkeypatched

---

## Step 3 — Cost logging

### What to do

`app/services/cost_service.py`: `record_cost_event(session, *, job_id,
sub_job_id, provider, operation, model_version, unit_cost_usd, units=1)` —
adds a `CostEvent` row, does not commit (caller's transaction, per
`docs/conventions.md`). `unit_cost_usd` comes from
`config.global.unit_cost_usd` (Step 0's schema addition), never hardcoded.

### Checkpoint 3

- [x] A cost event is recorded for a successful generation —
      `tests/integration/test_cost_service.py` +
      `tests/integration/test_generation_worker.py::test_mode_a_success_completes_sub_job`
- [x] A cost event is **also** recorded for a call that ends in
      `SAFETY_REFUSAL` — `docs/business-rules.md` §10: "cost is recorded even
      when the sub-job ends `REJECTED`" —
      `test_cost_service.py::test_cost_event_recorded_even_for_refused_call`
      and exercised for real in
      `test_generation_worker.py::test_safety_refusal_rejected_no_retry`
- [x] `config_versions.payload.global.unit_cost_usd` documented in
      `docs/schema.md`; `scripts/seed_dev.py`'s `CATEGORY_PAYLOAD` updated

---

## Step 4 — The Celery task

### What to do

`app/workers/generation.py`: `generation.transform_photo(sub_job_id: str)`
(naming per `docs/conventions.md`: `<module>.<verb>_<noun>`). Given a
sub-job:

1. Load `sub_job` + parent `job` + the job's **pinned** `config_versions`
   row (never the currently-active one — same pinning discipline as retry,
   `docs/business-rules.md` §5).
2. Resolve the category/angle prompt from the pinned config payload; append
   `default_negative_prompt`.
3. Mode A (`source_type == UPLOADED`): download the input asset's bytes from
   Storage. Mode B (`SYNTHETIC`): use the category's `reference_image_urls`.
4. `rate_limiter.acquire` — if the window is exhausted, treat as
   `RATE_LIMITED` (same failure path as a live 429, not a special case).
5. Generate a `seed` (random, recorded for reproducibility per
   `docs/ai-integration.md`).
6. Call `GeminiProvider.generate(...)`.
7. **Cost event written before the result is evaluated further** — success
   or `ProviderError`, both get a `CostEvent` row (Step 3).
8. On success: upload the returned bytes to `jewelry-outputs`
   (`storage_service`), create the `OUTPUT` `Asset` row, set
   `prompt_snapshot` / `model_version` / `seed` on the sub-job. Mode A →
   `SubJobStatus.COMPLETED`. Mode B → `SubJobStatus.QA_REVIEW`,
   `qa_status: NOT_APPLICABLE` (scoring is Phase 9 — see the reality-check
   section above).
9. On `ProviderError`: classify per `failure_class`. `SAFETY_REFUSAL` →
   `SubJobStatus.REJECTED` immediately, no retry. `RATE_LIMITED` /
   `TRANSIENT_PROVIDER` / `TRANSIENT_NETWORK` → retried, up to
   `MAX_ATTEMPTS = 3` total attempts, matching `docs/business-rules.md`
   §4's "3 attempts." **Deviation from the plan above:** implemented as a
   tight in-process loop inside `app/services/generation_service.py`
   (`transform_photo`), not literal Celery `autoretry_for`/`retry_backoff`.
   Observably equivalent for this phase's scope — `attempt_count` still
   reflects every attempt, the sub-job still only reaches `FAILED` once the
   budget is exhausted — and it's deterministically testable under
   `task_always_eager` without timing real backoff delays. No exponential
   jitter is applied between in-process attempts. If real backoff timing
   between attempts becomes necessary (e.g. once real rate limits are being
   hit at volume), revisit this as Celery-level retry. After the retry
   budget is exhausted → `SubJobStatus.FAILED`. `INTERNAL` →
   `SubJobStatus.FAILED`, `retryable` (client-facing, via the existing
   status/retry machinery from Phase 1/2/8) stays true per the
   failure-class table.
10. This task does **not** recompute parent job status — no caller wires it
    into a job yet (that's Phase 7's fan-out). Calling it directly against a
    seeded sub-job and asserting the sub-job's own state afterward is the
    correct test shape for this phase.

### Checkpoint 4

All verified in `tests/integration/test_generation_worker.py` against
testcontainers Postgres + real local Redis + real Supabase Storage
(only the Gemini call itself is faked):

- [x] Mode A success → sub-job `COMPLETED`, real `OUTPUT` asset with
      downloadable bytes, `prompt_snapshot`/`model_version`/`seed` all set —
      `test_mode_a_success_completes_sub_job`
- [x] Mode B success → sub-job `QA_REVIEW`, not `COMPLETED` — a synthetic
      angle is never auto-completed without a QA score
      (`docs/business-rules.md` hard invariant, `CLAUDE.md` Hard Rule 6) —
      `test_mode_b_success_lands_in_qa_review_not_completed`
- [x] Safety refusal → sub-job `REJECTED`, `failure_class: SAFETY_REFUSAL`,
      no retry attempted (`attempt_count == 1`), cost event still recorded —
      `test_safety_refusal_rejected_no_retry`
- [x] Transient failure exhausting all attempts → sub-job `FAILED`,
      `attempt_count == MAX_ATTEMPTS` —
      `test_transient_failure_exhausts_attempts_then_fails`
- [x] Rate-limit exhaustion (real Redis bucket pre-filled to capacity)
      routes through the same `RATE_LIMITED` path as a live 429 —
      `test_rate_limit_exhaustion_routes_through_rate_limited_path`
- [x] The task never imports `google.genai` directly (only
      `app/providers/gemini.py` does) —
      `test_generation_task_never_imports_google_genai_at_module_level`
- [x] Celery registration/routing verified —
      `tests/unit/test_generation_task_registration.py` (same pattern as
      Phase 3's `test_config_beat_schedule.py`). The task body itself is
      exercised by calling `generation_service.transform_photo` directly
      rather than through `celery_app`'s eager execution, because the
      thin task wrapper (`app/workers/generation.py`) binds to
      `settings.DATABASE_URL` at import time — pointed at the live Supabase
      project, not the ephemeral testcontainers DB a test spins up. This is
      the same constraint Phase 3/4's Celery tasks were tested under; the
      wrapper is trivial enough (session lifecycle only, `mypy --strict`
      clean) that this is a reasonable boundary, not a coverage gap.

---

## Step 5 — Self-audit

Same discipline as every prior phase: re-read every checkpoint above, verify
with real tests (testcontainers Postgres, real local Redis, real Supabase
Storage for the asset round-trip, fixture-driven Gemini responses — never a
live Gemini call), fix failures before declaring done, sync `docs/schema.md`
/ `docs/ai-integration.md` / `CLAUDE.md` / `phases/phase-roadmap.md`.

---

## Note for Phase 7

Phase 7 (Orchestration & Partial Success) is what actually calls
`generation.transform_photo` from a real job — Celery group fan-out per
angle, a chord callback that recomputes parent status
(`app/services/status_rollup.py`, already built and tested in Phase 2) using
the same transaction discipline this phase's task followed for its own row.
Until Phase 7 exists, a job created via `/generate` sits at `PENDING`
forever — expected, not a regression.
