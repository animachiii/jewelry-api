# Phase 1 — API Contract & Mock Server

## Objective

Freeze the complete v2 API contract as an OpenAPI 3.1 spec and ship a mock server that
returns realistic fixtures for every state the Flutter ERP can encounter — including
`PARTIAL_SUCCESS`, non-retryable rejections, and QA review. The Flutter team builds
against this from day one and is never blocked waiting on Celery workers.

This is the client sign-off gate. Everything after this phase implements a contract that
has already been agreed, rather than negotiating the contract while implementing it.

## Context

**Why this replaces `phase-1-ui.md`.** The v1 project template put UI conversion here
because the UI was the artifact the client signed off on before real work started. This
project has no UI in the repository — the consumer is the client's Flutter ERP, built by
another team. The API contract plays exactly that role: it is what the client approves and
what unblocks the other team to work in parallel.

**From Phase 0, available now:**

- FastAPI app scaffold, Pydantic v2 `Settings`, layered folder structure
- Full schema migrated to Supabase Postgres; SQLAlchemy models in `app/db/models/`
- Three private Storage buckets with working signed-URL generation
- Redis + Celery with `gpu` / `io` queues (no real tasks yet)
- Seed data covering all 8 job scenarios — **these are the source of the mock fixtures**
- Test harness and green CI

**Not yet built and deliberately not needed here:** config sync, matting, generation, QA,
orchestration, retry execution. This phase produces the contract and the mock, not the
implementation.

---

## Step 1 — Pydantic schemas and error envelope

### What to do

Create `app/api/v2/schemas/` with request and response models for every route in
@docs/api-routes.md. Naming per @docs/conventions.md: `<Noun><Request|Response>`.

Request models: `GenerateJobRequest`, `PresignUploadRequest`, `QaDecisionRequest`.
Response models: `ConfigResponse`, `PresignUploadResponse`, `JobAcceptedResponse`,
`JobStatusResponse`, `AngleStatus`, `JobListResponse`, `JobCostResponse`, `ErrorResponse`.

`GenerateJobRequest.angles` is the tricky one. It is a mapping from angle name to a
discriminated union of three shapes — uploaded (`storage_path`), synthetic
(`synthetic: true`), skipped (`skip: true`). Model it as a proper Pydantic discriminated
union so invalid combinations fail at parse time with a useful message, not in a service
method with a generic 500.

`AngleStatus` must carry: `angle`, `status`, `source_type`, `synthetic`, `image_url`
(nullable), `qa_status`, `qa_score` (nullable), `failure_class` (nullable),
`error_message` (nullable), `retryable`, `retry_url` (nullable).

Implement `app/core/errors.py`: the `AppError` hierarchy from @docs/conventions.md, the
`ErrorResponse` envelope, and FastAPI exception handlers that convert every `AppError`
into the envelope. Add a catch-all handler that returns `INTERNAL_ERROR` with the
`request_id` and logs the traceback — never the exception text to the client.

Add request-ID middleware: generate a ULID per request, bind it to structlog context,
return it in every response header and in every error body.

**Enumerate every error code now.** Write them as a Python `StrEnum` and list them in
@docs/api-routes.md. The Flutter ERP branches on these strings; adding one later is fine,
changing one is a breaking change. At minimum:

```
INVALID_API_KEY, INSUFFICIENT_SCOPE, CATEGORY_NOT_FOUND, CATEGORY_INACTIVE,
ANGLE_NOT_ENABLED, SYNTHETIC_NOT_ALLOWED, NO_ANGLES_REQUESTED, ASSET_NOT_FOUND,
ASSET_NOT_OWNED, IDEMPOTENCY_KEY_REQUIRED, IDEMPOTENCY_KEY_CONFLICT,
JOB_NOT_FOUND, SUBJOB_NOT_RETRYABLE, RETRY_LIMIT_EXCEEDED, INPUT_ASSET_EXPIRED,
RATE_LIMIT_EXCEEDED, QUOTA_EXCEEDED, CONFIG_UNAVAILABLE, INTERNAL_ERROR
```

### Checkpoint 1

- [ ] Every route in @docs/api-routes.md has request and response Pydantic models
- [ ] `GenerateJobRequest` rejects an angle object with both `skip: true` and
      `storage_path` set, with a 422 naming the offending angle
- [ ] `GenerateJobRequest` rejects an unknown angle key (e.g. `"BACK"`) with a 422
- [ ] Every `AppError` subclass produces the exact envelope shape in
      @docs/conventions.md, verified by a test per subclass
- [ ] An unhandled `ZeroDivisionError` in a test route returns `INTERNAL_ERROR` with a
      `request_id` and **no** exception text in the body
- [ ] `request_id` is present in the response header on both success and failure, and
      matches the value in the error body
- [ ] The error-code `StrEnum` contains every code listed above and is documented in
      @docs/api-routes.md
- [ ] `mypy --strict app/api/` exits 0

---

## Step 2 — Route stubs and OpenAPI generation

### What to do

Create route handlers for every endpoint in @docs/api-routes.md, registered under
`/api/v2`. Each has the correct method, path, auth dependency, response model, and
documented status codes — and a body that raises `NotImplementedError` or returns a
fixture (Step 3). **No business logic in this phase.**

Implement `app/core/auth.py` for real, not stubbed — it is small and everything else
depends on it. An `X-API-Key` dependency that looks up `api_clients` by `key_prefix`,
verifies the Argon2 hash, checks `is_active`, and attaches the client and its scope to the
request. A second dependency enforces `ops` scope.

Auth being real from Phase 1 means the Flutter team integrates against the actual auth
flow, not a mock they later have to redo.

Annotate every route with `responses={}` covering its documented error codes so the
generated spec shows them. Configure the OpenAPI metadata: title, version `2.0.0`,
description, server URLs, and the `X-API-Key` security scheme applied globally except on
`/health`.

Export the spec to `docs/openapi.json` via a script, and **check it into git**. A spec in
version control is a spec you can diff — which is how you catch an accidental breaking
change in review.

### Checkpoint 2

- [ ] Every route in @docs/api-routes.md exists at the documented method and path —
      verified by asserting against the route table, not by eye
- [ ] `GET /api/v2/health` returns 200 with **no** API key
- [ ] Every other route returns 401 `INVALID_API_KEY` with no key, and with a malformed key
- [ ] A revoked client's key (seeded in Phase 0) returns 401
- [ ] A `client`-scope key returns 403 `INSUFFICIENT_SCOPE` on `/api/v2/jobs` and
      `/api/v2/qa/review-queue`
- [ ] An `ops`-scope key succeeds on both
- [ ] `scripts/export_openapi.py` writes `docs/openapi.json`, and it validates against the
      OpenAPI 3.1 schema
- [ ] The spec's security scheme is `X-API-Key` on every path except `/health`
- [ ] Every route in the spec documents its error responses, not just 200
- [ ] `docs/openapi.json` is committed and CI fails if it is out of date with the code

---

## Step 3 — Mock fixtures for every state

### What to do

Add a `MOCK_MODE` setting. When enabled, routes return fixtures from the Phase 0 seed data
instead of raising `NotImplementedError`. Real handlers replace these in Phases 2–8; the
contract does not change when they do.

`GET /status/{job_id}` must return a realistic payload for **all eight** seeded scenarios,
selected by `job_id`. Give the Flutter team a documented table of which seeded `job_id`
produces which state — they need deterministic fixtures to build error handling against.

| Scenario | Parent status | What the ERP must render |
| :--- | :--- | :--- |
| 4/4 succeeded | `COMPLETED` | Four images |
| 3 ok, 1 `FAILED` | `PARTIAL_SUCCESS` | Three images + retry button on the failed slot |
| 3 ok, 1 `REJECTED` | `PARTIAL_SUCCESS` | Three images + **no retry button**, refusal message |
| 0/4 succeeded | `FAILED` | Error state, retry offered per angle |
| 1 angle, failed | `FAILED` | Not partial — single-angle failure is total failure |
| 2 requested, 2 skipped, both ok | `COMPLETED` | Two images, two empty slots |
| Synthetic in `QA_REVIEW` | `PROCESSING` | Still polling — no image yet |
| 2 done, 2 in flight | `PROCESSING` | Progressive render |

The third row is the one teams get wrong. `REJECTED` returns `retryable: false` and no
`retry_url`. The ERP must not render a retry button for a safety refusal — retrying a
deterministic refusal burns money and shows the user a button that will never work.

`image_url` values must be **real signed URLs** against the Phase 0 buckets, with the
production TTL. Fake `https://example.com/...` strings let the Flutter team build an image
pipeline that breaks the moment it meets a real expiring URL.

Also mock: `/config` from the active seeded config version, `/uploads/presign` returning
genuinely working upload URLs, `/generate` returning a 202 with a seeded `job_id`, and
`/retry` returning 202 or the appropriate 409.

Add `Retry-After` on non-terminal status responses and `poll_after_ms` on the `/generate`
202, so the ERP implements backoff from the start rather than hammering the endpoint.

### Checkpoint 3

- [ ] All 8 scenarios reachable by `job_id` and documented in a table in
      `docs/integration-guide.md`
- [ ] The `PARTIAL_SUCCESS` + `FAILED` fixture returns `retryable: true` and a working
      `retry_url` on exactly the failed angle
- [ ] The `PARTIAL_SUCCESS` + `REJECTED` fixture returns `retryable: false`, **no**
      `retry_url`, and a human-readable `error_message`
- [ ] The single-angle-failed fixture returns `FAILED`, not `PARTIAL_SUCCESS`
- [ ] The skipped-angles fixture returns `requested_angles: 2` and omits the skipped angles
      from success math
- [ ] The `QA_REVIEW` fixture returns parent `PROCESSING` with no `image_url` on the
      flagged angle
- [ ] Every `image_url` is a real signed URL that returns image bytes on GET
- [ ] Every `image_url` returns 403 after TTL expiry
- [ ] `/uploads/presign` returns a URL that accepts a real PUT
- [ ] Non-terminal status responses include `Retry-After`; terminal responses do not
- [ ] A `client`-scope key requesting another client's `job_id` gets **404, not 403**
- [ ] `MOCK_MODE=false` makes every unimplemented route raise rather than silently return
      fixtures — mock data must never reach production

---

## Step 4 — Contract handoff and sign-off

### What to do

Serve Swagger UI at `/docs` and ReDoc at `/redoc`, both gated to non-production.

Write `docs/integration-guide.md` for the Flutter team. Keep it practical:

- Auth: how to send the key, what 401 vs 403 mean
- The upload → generate → poll → retry lifecycle, as a sequence
- Polling strategy: honor `Retry-After`, exponential backoff, stop on terminal status
- **Handling `PARTIAL_SUCCESS`** — render what succeeded, don't discard it
- **When to show a retry button** — `retryable: true` only, never on `REJECTED`
- **How to display synthetic angles** — the `synthetic` flag must be visible to the user
- Signed URL expiry: cache the bytes, not the URL; re-poll status for a fresh URL
- The full error-code table with recommended UI treatment per code
- The seeded `job_id` → scenario table for local development

Deploy the mock server somewhere the Flutter team can reach it. Give them the base URL and
a `client`-scope key.

Then run the sign-off session. Walk the client and the Flutter lead through the contract,
specifically the partial-success and synthetic-angle behavior — those two change what the
ERP has to build, and both are easier to renegotiate now than in Phase 7.

### Checkpoint 4

- [ ] `/docs` and `/redoc` render the full spec, with auth applied
- [ ] Both are unreachable when `APP_ENV=production`
- [ ] `docs/integration-guide.md` covers every section above
- [ ] Mock server deployed and reachable by the Flutter team from their environment
- [ ] Flutter team has a working `client`-scope key and has successfully called
      `/config`, `/uploads/presign`, `/generate`, and `/status` against it
- [ ] Flutter lead has confirmed in writing that `PARTIAL_SUCCESS` rendering and the
      retryable/non-retryable distinction are understood and buildable
- [ ] Client has confirmed in writing that synthetic angles will be visually flagged in the
      ERP
- [ ] **Contract frozen:** `docs/openapi.json` tagged `v2.0.0-contract` in git

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file
2. Test each one: call the route with a real HTTP client, fetch the signed URL, check the
   status code, validate the spec. Do not mark a checkpoint from reading the code
3. Return a structured report:

   ```
   ✅ [Checkpoint] — Pass
   ⚠️ [Checkpoint] — Partial: [specific reason]
   ❌ [Checkpoint] — Fail: [specific reason]
   ```

4. Fix all failures and partials before reporting phase complete
5. If anything in this phase changed the schema, routes, or business rules from what's
   documented in `docs/`, update the relevant `docs/*.md` file now — before declaring the
   phase complete. `claude.md` and `docs/` must reflect reality, not the original plan.
   Sign-off feedback that changed the contract **must** land in @docs/api-routes.md and
   @docs/business-rules.md before this phase closes
6. Only say "Phase 1 Complete" when every checkbox is green and docs are in sync

---

## Final Phase 1 Checklist

- [ ] Pydantic schemas for every route; discriminated union on the angles payload
- [ ] Error envelope, exception hierarchy, request-ID middleware, and a frozen error-code enum
- [ ] All routes registered with real API-key auth and scope enforcement
- [ ] OpenAPI 3.1 spec generated, validated, committed, and diff-checked in CI
- [ ] Mock fixtures covering all 8 job states with real signed URLs
- [ ] Integration guide written and mock server deployed for the Flutter team
- [ ] Client and Flutter lead sign-off received in writing; contract tagged in git
- [ ] Self-audit passed with all green
- [ ] `docs/` updated to match what was actually built
- [ ] Manual verification done by architect

---

## Note for Phase 2

Do not start Phase 2 until the contract is signed off. The entire value of this phase is
that later phases implement an agreed contract instead of discovering it. If sign-off is
delayed, Phase 2 (Data Model & Job State Machine) is the safest thing to start on, since
the schema is already migrated and independent of contract details.
