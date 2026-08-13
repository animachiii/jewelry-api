# Background-operation QA preview + retry-QA-only

**Date:** 2026-08-13
**Status:** Approved, implementing

## Problem

A `BACKGROUND_REMOVAL`/`BACKGROUND_REPLACEMENT` job that lands in
`QA_REVIEW`/`FLAGGED` is currently a dead end for the client: `GET /status`
never returns `image_url` for a non-`COMPLETED` sub-job
(`docs/business-rules.md` §7/§12's deliberate "never show a flagged image
before human review" rule), and `POST /jobs/{job_id}/retry` only fires on
`FAILED` (`docs/business-rules.md` §5). A job whose generation actually
succeeded but whose QA judge call failed (real incident, 2026-08-13:
`qa_score: NULL`, `job_events.detail.outcome: "provider_error"`) or whose
score just fell under the uncalibrated `0.92` placeholder threshold has no
way to be seen or recovered short of the ops-only `/qa/review-queue` +
`/qa/{sub_job_id}/decision` routes, which have no UI anywhere yet.

## Scope

Background-operation jobs only. Synthetic angle generation
(`score_synthetic_angle`) has the identical gap but is not touched here —
noted as a future extension if it turns out to matter.

## Design

### 1. `preview_image_url` (new field, `image_url` unchanged)

`BackgroundResultStatus` (`app/api/v2/schemas/status.py`) gets a new
optional field, populated by `status_service.build_background_result_status`
from the output asset whenever one exists — independent of `status`. Real
ERP client behavior is untouched: `image_url` still only appears on
`COMPLETED`, exactly as `docs/api-routes.md` already documents. A client
that doesn't know about `preview_image_url` sees no behavior change at all.

### 2. Retry-QA-only, same endpoint

`POST /jobs/{job_id}/retry` gains a second precondition branch. Today
`check_retry_preconditions` (`app/services/job_service.py`) only accepts
`FAILED`. A new `check_qa_retry_preconditions` accepts `QA_REVIEW` +
`qa_status: FLAGGED`, under the same `MAX_RETRY_ATTEMPTS` ceiling
(`sub_job.attempt_count`, incremented on dispatch — one shared counter,
matching this codebase's existing precedent rather than a new column).

`app/api/v2/retry.py::retry_job` tries the FAILED-retry path first (existing
behavior, unchanged); if that doesn't apply, tries the QA-retry path. A new
`retry_service.execute_qa_retry(session, sub_job)` records a
`QA_RETRY_REQUESTED` job_event and increments `attempt_count` — it does
**not** touch `sub_job.status` (stays `QA_REVIEW`) or `job.status`, and does
**not** dispatch `background.process` (no regeneration, no second billed
Gemini call) — only `qa.score_background.delay(sub_job_id)`, re-running
just the judge call against the image that already exists.

`status_service.build_background_result_status`'s `retryable`/`retry_url`
computation extends to cover this case too, so the existing `retry_url`
field in the status response just works for both kinds of retry — the
client (or `/ui`) doesn't need to know which kind it's asking for.

### 3. `/ui` page

Shows `preview_image_url` (falling back from `image_url`) whenever present,
with a "not yet approved" marker when `qa_status: FLAGGED`, and a "Retry QA"
button wired to the existing `retry_url`.

## Out of scope

- Synthetic angle generation's identical gap (`score_synthetic_angle`).
- Calibrating `background_qa_similarity_threshold` (`0.92`, still a
  documented placeholder) — orthogonal to this fix.
- The ops `/qa/review-queue` UI — this gives the *client* a way to retry
  QA scoring, not a human-review workflow.

## Testing

- Unit: `check_qa_retry_preconditions` (accepts `QA_REVIEW`+`FLAGGED`,
  rejects everything else including a *low-score* `QA_REVIEW` that's
  `PASSED` — shouldn't happen but worth pinning), `execute_qa_retry`
  (event recorded, `attempt_count` incremented, status untouched).
- Unit: `status_service.build_background_result_status` — `preview_image_url`
  present for `QA_REVIEW`, absent when no output asset exists yet (e.g.
  still `GENERATING`); `retryable`/`retry_url` true for `QA_REVIEW`+`FLAGGED`.
- Integration: flag a job with a low-score QA fixture, call
  `POST /jobs/{job_id}/retry`, confirm it dispatches `qa.score_background`
  (not `background.process`), and — using a high-score fixture the second
  time — confirm the job reaches `COMPLETED`. Same fixture-driven pattern
  `tests/integration/test_background_operations.py` already uses.
