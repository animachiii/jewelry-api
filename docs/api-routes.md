# API Routes

**Base path:** `/api/v2`
**Auth:** `X-API-Key: <key>` header on every route except `/health`.
**Content type:** `application/json` unless noted.
**Errors:** every non-2xx returns the standard envelope — see @docs/conventions.md.

---

## Health

### `GET /api/v2/health`
Liveness and dependency check. **No auth.**

Returns `200` with per-dependency status (`db`, `redis`, `storage`, `active_config_version`).
Returns `503` if Postgres or Redis is unreachable. A stale Sheets sync is `degraded`, not
unhealthy — the system runs fine on the last active config version.

---

## Config

### `GET /api/v2/config`
Returns the active angle matrix: all categories, which angles are enabled per category,
and which angles permit synthetic generation. **Auth required.**

Served from the Redis `config:active` cache; falls back to the active `config_versions`
row in Postgres on cache miss. Never calls Google Sheets inline.

Response includes `config_version` — the client should send this back on `/generate` so
mismatches can be detected.

Prompts and reference image URLs are **not** exposed to the client. Internal only.

### `POST /api/v2/internal/config/sync`
Pulls Google Sheets, normalizes, hashes, and writes a new `config_versions` row if the
hash changed. Activates it and invalidates the Redis cache. **Ops-only auth scope.**

Idempotent: an unchanged sheet returns the existing active version and creates nothing.
Also runs on a Celery beat schedule (`CONFIG_SYNC_CRON`, Celery task `config.sync`).

Returns `202` with `{ "config_version": int, "sync_status": "SUCCESS" | "FAILED",
"activated": bool }`. `sync_status`/`activated` describe the version the response
refers to, not necessarily a version created by this call — see the three outcomes
below.

Three distinct outcomes, all `202` (this is an accepted-and-resolved sync, not an
error path unless both Redis and Postgres are down — see docs/business-rules.md §9):

1. **Sheets unreachable or not configured** — no new row is written (there is no
   payload to record); returns the currently active version unchanged,
   `activated: true`. This is the path exercised in every environment today, since
   no real Google Sheets project exists yet (see phases/phase-3-config-service.md).
2. **Sheets reachable, payload fails validation** — a new row is written with
   `sync_status: FAILED` and `error_message` set, `is_active` stays `false`; the
   response reports the *previous* (still active) version, not the failed one.
3. **Sheets reachable, payload valid, hash changed** — a new row is written,
   activated, and the Redis cache is invalidated; response reports the new version.

---

## Uploads

### `POST /api/v2/uploads/presign`
Returns short-lived signed upload URLs for the Supabase `jewelry-inputs` bucket, one per
angle the client intends to submit. **Auth required.**

Request names the category and the angles. Response gives, per angle, an `upload_url`,
the `storage_path` to quote back on `/generate`, and an expiry.

The client uploads directly to Supabase Storage — image bytes never pass through the API.
This is what keeps `/generate` fast and avoids request-size limits.

---

## Generation

### `POST /api/v2/generate`
The fat payload. Creates a parent job and fans out up to four sub-jobs.
**Auth required. `Idempotency-Key` header required.**

Request contains: `category_code`, optional `sku_reference`, optional `metadata`, and an
`angles` object keyed by angle name. Each angle is one of:

- `{ "storage_path": "..." }` — a real uploaded photograph
- `{ "synthetic": true }` — generate from the reference matrix, no source photo
- `{ "skip": true }` — not requested

Returns `202` with `job_id`, `status: PENDING`, the resolved per-angle plan, and a
`poll_after_ms` hint.

**Validation, in order — all failures are `400`/`422` before any job row is created:**

1. `category_code` exists and is active in the current config version
2. Every requested angle is `enabled` for that category
3. Any angle marked `synthetic` has `synthetic_allowed: true` for that category
4. Every `storage_path` exists in the bucket and belongs to this client
5. At least one angle is not skipped

**Idempotency:** if `(client_id, Idempotency-Key)` already exists, return the original
job's `202` response unchanged. Do not create a second job. Do not bill twice.

Returns `429` when the client's rate limit or daily quota is exceeded, with `Retry-After`.

### `GET /api/v2/status/{job_id}`
Poll for job state. **Auth required. Scoped to the owning client — another client's
`job_id` returns `404`, never `403`.**

Returns the parent `status` (`PENDING` | `PROCESSING` | `COMPLETED` | `PARTIAL_SUCCESS` |
`FAILED`), counts, and an `angles` array. Each angle carries:

- `status`, `source_type`, `synthetic` flag
- `image_url` — a **freshly signed URL, 1-hour TTL**, present only when `COMPLETED`
- `qa_status` and `qa_score` when applicable
- `failure_class`, `error_message`, and `retryable` when failed
- `retry_url` when `retryable` is true

`retryable` is `false` for `REJECTED` — a safety refusal or QA rejection will not resolve
by retrying, and the ERP must not offer the button.

Response sets `Retry-After` while the job is non-terminal so the ERP can back off.

### `POST /api/v2/jobs/{job_id}/angles/{angle}/retry`
Re-runs one failed angle. **Auth required. `Idempotency-Key` header required.**

Preconditions — all return `409` with a specific code:

- Sub-job status must be `FAILED` (not `REJECTED`, not `COMPLETED`, not in flight)
- `attempt_count` must be below the retry ceiling (see @docs/business-rules.md)
- The input asset (or matte) must still exist and be unexpired

On success: resets the sub-job to `PENDING`, increments `attempt_count`, re-enqueues, and
recomputes the parent status. Returns `202`.

The retry reuses the **original** `config_version_id` pinned on the job, not the currently
active one. A retried angle must match the three that already succeeded.

---

## Ops

### `GET /api/v2/jobs`
Paginated job list, filterable by status, category, and date range. **Ops-only scope.**

### `GET /api/v2/jobs/{job_id}/cost`
Aggregated `cost_events` for a job, broken down per angle and attempt. **Ops-only scope.**

### `GET /api/v2/qa/review-queue`
Sub-jobs in `QA_REVIEW` — synthetic outputs that fell below the similarity threshold and
need a human decision. **Ops-only scope.**

### `POST /api/v2/qa/{sub_job_id}/decision`
Approve or reject a flagged output. Approval moves the sub-job to `COMPLETED`; rejection
moves it to `REJECTED` with `failure_class: QA_REJECTED`. Both recompute parent status.
**Ops-only scope.**

---

## Auth scopes

| Scope | Routes |
| :--- | :--- |
| `client` | config, uploads, generate, status, retry |
| `ops` | everything in `client` plus `/internal/*`, `/jobs`, `/qa/*` |

Scope lives on the `api_clients` row. There are exactly two scopes — resist adding more
until there is a concrete need.

---

## Status codes used

| Code | Meaning here |
| :--- | :--- |
| `200` | Read succeeded |
| `202` | Job or retry accepted and queued |
| `400` | Malformed request |
| `401` | Missing or invalid API key |
| `403` | Valid key, insufficient scope |
| `404` | Not found, or not owned by this client |
| `409` | State conflict (retry preconditions) |
| `422` | Well-formed but semantically invalid (bad category, disabled angle) |
| `429` | Rate limit or quota exceeded |
| `503` | Dependency down |
