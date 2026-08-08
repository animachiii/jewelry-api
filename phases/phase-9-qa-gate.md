# Phase 9 — Output QA Gate

## Reality check before writing this

What already exists, verified by reading the code, not assumed:

- `sub_jobs.qa_score` / `qa_status` columns exist since Phase 0
  (`qa_status_t` enum: `NOT_APPLICABLE | PASSED | FLAGGED | FAILED`).
- `generation_service.transform_photo` (Phase 6/7) already lands a
  successful `SYNTHETIC` sub-job in `QA_REVIEW`, `qa_status` still its
  default `NOT_APPLICABLE` — deliberately unscored, per Phase 6's own note
  ("Phase 9 owns actual scoring, this phase deliberately does not preempt
  it"). Real-photo (`UPLOADED`) sub-jobs never enter `QA_REVIEW` at all —
  `docs/ai-integration.md`: "No QA gate on Mode A," a deliberate accepted
  risk (`docs/decisions/0001-drop-local-matting.md`), not something this
  phase changes.
- `app/api/v2/qa.py`'s two routes and `app/workers/qa.py` are still
  `NotImplementedError`/empty stubs from Phase 0/1. Nothing scores anything,
  nothing decides anything.
- `config_versions.payload.global.qa_similarity_threshold` already exists
  (`0.82`, seeded), per Phase 6's note: "a placeholder until calibrated
  against real client pieces." **This phase does not calibrate it** — no
  real client pieces exist in this environment (same situation Phase 6 hit
  with `GEMINI_API_KEY`, Phase 3 hit with the Sheets column layout). Roadmap
  open decision #8 ("Has the client seen and accepted synthetic-angle
  output quality?") stays open; calibration needs real scored examples,
  which needs a live client review pass, not something a phase can
  self-audit into existence.

**What this phase actually builds**, matching the roadmap line for real:

1. A `QaProvider` abstraction (mirrors `GenerationProvider`, Phase 6) and a
   `GeminiQaProvider` implementation — an LLM-judged similarity score,
   per `docs/ai-integration.md` Call Site 2's own noted default ("an
   LLM-judged similarity check (Gemini) is the likely default... Undecided
   until Phase 9" — decided here, in favor of the option the docs already
   flagged as likely, not a dedicated embedding model, to avoid pulling in
   a second model dependency for a placeholder threshold).
2. Real automatic scoring wired into the pipeline: the moment a synthetic
   sub-job lands in `QA_REVIEW`, it gets scored — not left inert until
   someone happens to call the review-queue endpoint.
3. Real `GET /qa/review-queue` and `POST /qa/{sub_job_id}/decision`.

**A design gap found and closed, not in the roadmap line:** `docs/business-
rules.md` §7 covers exactly two outcomes of an *attempted* score (above/
below threshold) and two outcomes of a *human decision* (approve/reject).
It says nothing about the QA provider call itself failing (timeout,
malformed response, refusal). Silently marking a sub-job `COMPLETED` when
scoring couldn't run would defeat the entire point of this gate ("the only
mechanism in the system that catches a silent failure" —
`docs/ai-integration.md`). This phase's decision, made explicit here: a QA
provider failure never auto-completes and never auto-rejects — it lands the
sub-job in `QA_REVIEW` / `qa_status: FLAGGED` with `qa_score: NULL`, the
same human-review path as a genuinely-low score. Failing open to a human,
never to an unscored pass.

**Reused, not rebuilt:** `generation_service._recompute_parent_status`
(Phase 7) is renamed `recompute_parent_status` (drop the leading
underscore) and imported by the new `qa_service.py` — same recompute-in-the-
same-transaction pattern Phase 7 and Phase 8 already established, no new
rollup logic needed. `status_rollup.compute_parent_status` already excludes
`QA_REVIEW` from both `S` and `F` by construction (Phase 2), so nothing
here needs to special-case it.

**Not built, deliberately:** no `cost_events` row for a QA call. Both
`docs/business-rules.md` §10 and `docs/ai-integration.md` only ever
describe billing for *generation* calls; QA scoring is never mentioned as a
billed operation in either doc. Adding cost tracking for it would be
inventing a business rule, not implementing one — flagged here rather than
silently added or silently skipped.

---

## Step 1 — `QaProvider` abstraction + `GeminiQaProvider`

### What to do

`app/providers/qa_base.py`: `QaResult` (`score: float`, `model_version:
str`) and `QaProvider(ABC)` with `score(output_image: bytes,
reference_images: list[bytes]) -> QaResult`, raising
`app.core.errors.ProviderError` on failure — same contract shape as
`GenerationProvider` (Phase 6).

`app/providers/gemini_qa.py`: `GeminiQaProvider`, same isolation pattern as
`app/providers/gemini.py` — `_call_api` is the real `google-genai` call
(prompts the model to return a JSON similarity score), never exercised in
tests (no real `GEMINI_API_KEY`); `score()` is the testable seam. Prompts
the judge model for `{"similarity_score": <0-1 float>, "reasoning": "..."}`
against the reference images; `_parse_response` extracts and range-validates
`similarity_score`, raising `ProviderError(failure_class=INTERNAL)` for a
missing/out-of-range/non-numeric value or an empty candidate — mirrors
`GeminiProvider._parse_response`'s malformed-response handling. Same
status-code classification (`429` → `RATE_LIMITED`, `5xx` →
`TRANSIENT_PROVIDER`, timeout/connection → `TRANSIENT_NETWORK`) — small,
deliberate duplication of `GeminiProvider`'s classifier rather than a
shared base, matching how `app/providers/sheets.py` and
`app/providers/gemini.py` already don't share one either.

New fixtures, `tests/fixtures/qa/`: `high_similarity.json` (score above
0.82), `low_similarity.json` (below), `malformed.json` (missing/invalid
score field).

### Checkpoint 1

- [ ] `GeminiQaProvider.score()` on a high-similarity fixture returns a
      `QaResult` with the expected score
- [ ] A malformed fixture (missing/non-numeric/out-of-range
      `similarity_score`) raises `ProviderError(failure_class=INTERNAL)`
- [ ] `429`/`5xx`/timeout fixtures classify the same as
      `GeminiProvider`'s equivalents

---

## Step 2 — Real automatic scoring, wired into the pipeline

### What to do

`app/services/generation_service.py`: rename `_recompute_parent_status` to
`recompute_parent_status` (update its two call sites in the same file).

New `app/services/qa_service.py`:

- `score_synthetic_angle(session, sub_job_id) -> SubJob`: loads the
  sub-job (must be `QA_REVIEW`), its job, pinned `config_version`, and
  category/angle config (`job_service.find_category`, same as
  `generation_service`). Downloads the output asset's bytes
  (`storage_service.download_to_temp`, via `sub_job.output_asset_id`) and
  the category's `reference_image_urls`
  (`generation_service.fetch_reference_images`, reused as-is). Calls
  `GeminiQaProvider.score(...)`.
  - **Success, `score >= qa_similarity_threshold`:** `qa_score = score`,
    `qa_status = PASSED`, `sub_job.status = COMPLETED`, then
    `recompute_parent_status` in the same transaction.
  - **Success, `score < threshold`:** `qa_score = score`,
    `qa_status = FLAGGED`. Status stays `QA_REVIEW` — no parent recompute
    needed (`compute_parent_status` already excludes `QA_REVIEW` from `S`
    and `F`, so the parent is already correctly `PROCESSING`).
  - **`ProviderError` from the QA call itself:** `qa_score` stays `NULL`,
    `qa_status = FLAGGED`, status stays `QA_REVIEW` — see the reality-check
    section above (fail open to human review, never to an unscored pass).
  - Records a `QA_SCORED` `JobEvent` in every branch (`detail`: `score`
    when available, `threshold`, `outcome`).

`app/workers/qa.py`: `qa.score_similarity(sub_job_id: str)` — thin Celery
task, same per-call-engine split as every worker since Phase 4
(`app/workers/generation.py`, `app/workers/retention.py`).

`app/workers/generation.py`: after `transform_photo` returns and its
transaction commits, if the returned sub-job's status is `QA_REVIEW`,
dispatch `qa.score_similarity.delay(str(sub_job.id))` — same
dispatch-after-commit placement `orchestration.fan_out_job` already uses
(Phase 7), not inside the service function itself, so a QA dispatch never
reads a sub-job row before its own creating transaction has landed.

### Checkpoint 2

- [ ] A synthetic angle scoring above threshold → `COMPLETED`,
      `qa_status: PASSED`, `qa_score` persisted, parent recomputes to
      `COMPLETED` (or `PARTIAL_SUCCESS` if another angle already failed)
- [ ] A synthetic angle scoring below threshold → stays `QA_REVIEW`,
      `qa_status: FLAGGED`, parent stays `PROCESSING`
- [ ] A QA provider failure (malformed-response fixture) → stays
      `QA_REVIEW`, `qa_status: FLAGGED`, `qa_score: NULL` — never
      `COMPLETED`, never `REJECTED`
- [ ] A real-photo (`UPLOADED`) angle never gets a `qa.score_similarity`
      dispatch — true by construction (only a `QA_REVIEW` result triggers
      it, and only `SYNTHETIC` success lands there)
- [ ] `QA_SCORED` `JobEvent` recorded for every branch above

---

## Step 3 — Real review queue + decision endpoints

### What to do

Add two `ErrorCode` values (`app/core/errors.py`): `SUB_JOB_NOT_FOUND`
(404) and `QA_NOT_PENDING` (409) — no existing code fits either case
(`JOB_NOT_FOUND` is job-scoped; `SUBJOB_NOT_RETRYABLE` is retry-specific
wording that would mislead here).

`app/services/qa_service.py`, continued:

- `get_review_queue(session) -> list[SubJob]` (or a small assembled DTO):
  sub-jobs where `status == QA_REVIEW and qa_status == FLAGGED` — narrower
  than "all `QA_REVIEW`" on purpose, matching `docs/api-routes.md`'s own
  wording ("synthetic outputs that fell below the similarity threshold")
  rather than the roadmap line's looser phrasing; a sub-job mid-scoring
  (not yet `FLAGGED` or `PASSED`) shouldn't appear in a human queue it'll
  likely never need.
- `submit_qa_decision(session, sub_job_id, decision) -> SubJob`: 404 if the
  sub-job doesn't exist; 409 `QA_NOT_PENDING` if its status isn't
  `QA_REVIEW`. `approve` → `qa_status = PASSED`, `status = COMPLETED`.
  `reject` → `qa_status = FAILED`, `status = REJECTED`,
  `failure_class = QA_REJECTED` (`docs/business-rules.md` §7's table,
  exactly). Both record a `QA_DECISION` `JobEvent` and call
  `recompute_parent_status` in the same transaction.

`app/api/v2/qa.py`: wire both routes to the service functions above,
building `QaReviewItem`/`QaReviewQueueResponse` (existing schemas,
`app/api/v2/schemas/qa.py`) — `image_url` is the flagged output's signed
URL (`storage_service.generate_signed_url`, same 1-hour-TTL pattern as
`status_service`), `reference_image_urls` come from the job's **pinned**
`config_version` (never the currently-active one — same rule retry already
follows for the same reason: visual consistency against what was actually
generated).

### Checkpoint 3

- [ ] `GET /qa/review-queue` returns only `QA_REVIEW`+`FLAGGED` sub-jobs,
      each with a real signed `image_url` that downloads real bytes
- [ ] `POST /qa/{sub_job_id}/decision` with `approve` on a `FLAGGED`
      sub-job → `202`, `COMPLETED`, `qa_status: PASSED`, parent recomputes
      correctly (including to a terminal status if this was the last
      pending angle)
- [ ] `POST /qa/{sub_job_id}/decision` with `reject` → `202`, `REJECTED`,
      `qa_status: FAILED`, `failure_class: QA_REJECTED`, parent recomputes
- [ ] Decision on a sub-job not in `QA_REVIEW` (e.g. already `COMPLETED`)
      → `409 QA_NOT_PENDING`
- [ ] Decision on a nonexistent `sub_job_id` → `404 SUB_JOB_NOT_FOUND`
- [ ] `client`-scope key on either route → `403` (existing auth wiring,
      confirmed still correct now that the routes do real work)
- [ ] A `REJECTED`-via-QA sub-job cannot be retried —
      `check_retry_preconditions` already refuses anything but `FAILED`
      (Phase 5/8), confirmed with a real request through `/retry`, not
      just read from the code

---

## Step 4 — Self-audit

Same discipline as every prior phase: re-read every checkpoint with real
tests (testcontainers Postgres, real local Redis, real Supabase Storage,
fixture-driven Gemini/QA — same stack Phase 6-8 used), fix failures before
declaring done. Sync `docs/ai-integration.md` Call Site 2 (mark it built,
LLM-judge decided over embedding model), `docs/schema.md` if
`qa_similarity_threshold`'s placeholder note needs updating (it doesn't —
still uncalibrated), `CLAUDE.md`, `phases/phase-roadmap.md`. Confirm
`tests/integration/test_api_contract.py::test_ops_scope_key_succeeds_past_auth_on_ops_routes`
still passes now that the QA routes do real work instead of raising
`NotImplementedError` (its assertion — status not `401`/`403` — was always
compatible with either, but worth confirming, not assuming).

---

## Note for later phases

Phase 10 (Auth & Security Hardening) should double check `ops`-scope
enforcement on both QA routes stays intact — nothing in this phase changes
`require_ops_scope`, but it's now gating routes that actually mutate state,
raising the cost of a scope bug. Phase 11 (Observability) is the natural
place to revisit whether QA calls should get cost tracking after all, once
real usage data exists to show whether that gap matters in practice.
