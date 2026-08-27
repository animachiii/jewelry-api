# Integration Guide — Flutter ERP

This is the practical guide for integrating against the v2 API. It assumes
you've read the shape of things in `docs/api-routes.md`; this document is
about *how to call it correctly*, not what every field means.

**Base URL (mock/dev):** ask your engineering contact for the current tunnel
or deployment URL — see "Where this is hosted" below.
**Spec:** `docs/openapi.json` (OpenAPI 3.1), also served live at `/openapi.json`,
with Swagger UI at `/docs` and ReDoc at `/redoc` (non-production only).

---

## 1. Auth

Every route except `GET /health` requires an `X-API-Key` header.

```
X-API-Key: O1_pSgS_CjNAJOyFlexFwSaootlsX-kM8t9bJsAup_4
```

This is a real `client`-scope key, seeded specifically for this handoff and
already wired to the 8 demo jobs in the table below. It is shown once, here —
there is no way to retrieve it again; if it leaks or you need a second one,
ask an engineer to mint a fresh row via `scripts/seed_dev.py`'s
`seed_api_clients` pattern.

- **401 `INVALID_API_KEY`** — the header is missing, malformed, doesn't match
  any active key, or matches a revoked one. Treat this as "stop and check
  configuration," not something to retry.
- **403 `INSUFFICIENT_SCOPE`** — your key is valid but doesn't have the scope
  the route needs (this only applies to the `ops` routes — everything in this
  guide uses `client` scope and won't hit it).

## 2. Lifecycle

```
POST /uploads/presign  → per-angle signed upload URLs
PUT  <upload_url>       → client uploads bytes directly to Supabase Storage
POST /generate          → 202, job_id, poll_after_ms
GET  /status/{job_id}   → poll until terminal
POST /jobs/{job_id}/angles/{angle}/retry → only for a FAILED (not REJECTED) angle
```

Image bytes never pass through the API — you PUT directly to the signed
`upload_url` from `/uploads/presign`, then quote the returned `storage_path`
back on `/generate`.

## 2b. Background operations (Phase 15)

`BACKGROUND_REMOVAL` and `BACKGROUND_REPLACEMENT` are a separate, simpler
lifecycle — one photo in, one photo out, no category or angles involved:

```
POST /uploads/presign  { "operation": "BACKGROUND_REMOVAL" }  → operation_upload
PUT  <upload_url>       → same direct-to-Storage upload as the angle flow
POST /background/remove  { storage_path, sku_reference?, metadata? }
  -- or --
POST /background/replace { storage_path, preset_code, sku_reference?, metadata? }
GET  /status/{job_id}   → poll until terminal, same as the angle flow
POST /jobs/{job_id}/retry → only for a FAILED (not REJECTED) job — no {angle} segment
```

`preset_code` for `/background/replace` must be one of `GET /config`'s
`background_presets` (`code` + `name` only — render it as a picker).

**Read `operation` first.** `GET /status/{job_id}` now returns an `operation`
field on every job (`ANGLE_GENERATION` | `BACKGROUND_REMOVAL` |
`BACKGROUND_REPLACEMENT`) — check it before deciding whether to read `angles`
or `results`:

- `operation: "ANGLE_GENERATION"` → read `angles` exactly as before. This is
  unchanged from what you already integrated against — `results` will be `[]`.
- `operation: "BACKGROUND_REMOVAL"` or `"BACKGROUND_REPLACEMENT"` → read
  `results` instead (one element — a background job has exactly one output).
  `angles` will be `[]`. Each item in `results` carries the same fields an
  angle status does (`status`, `source_type`, `image_url`, `qa_status`,
  `qa_score`, `failure_class`, `error_message`, `retryable`, `retry_url`)
  **except `angle` itself** — there isn't one.
- A `QA_REVIEW` result (or angle) means the job is still processing from your
  point of view — **do not render it as an error.** It means a human is
  reviewing it; keep polling. Background operations pass through `QA_REVIEW`
  on every successful generation, not just occasionally.
- `retryable`/`retry_url` on a `results` item point at
  `POST /jobs/{job_id}/retry` (no `{angle}` segment), not the angle retry
  route.

Everything else — polling strategy, `PARTIAL_SUCCESS` handling (structurally
unreachable for a background job, since it only ever has one sub-job),
signed URL expiry, error-code table — is identical to the angle flow below.

## 3. Polling strategy

- Honor the `Retry-After` header (seconds) on non-terminal `GET /status`
  responses, and `poll_after_ms` on the `/generate` `202`. Both exist so you
  implement backoff from day one instead of hammering the endpoint.
- Stop polling once `status` is one of `COMPLETED`, `PARTIAL_SUCCESS`, or
  `FAILED`. `Retry-After` is absent on terminal responses — its absence is
  itself a signal, but don't rely on that alone; check `status`.
- A successful retry moves a terminal job back to `PROCESSING` — resume
  polling after calling `/retry`.
- **Give the poll loop a budget, and never let one request overlap the
  next.** "Poll until terminal" above is not the whole rule: a job can stop
  advancing without ever reaching a terminal status (a worker lost to a
  container restart leaves a sub-job orphaned — see the reconciliation sweep
  in `docs/business-rules.md`). Cap the loop on both consecutive errors and
  total elapsed time, and schedule the next poll only after the previous
  response lands, rather than on a fixed interval — a fixed interval against
  a slow instance stacks requests in flight and makes it slower. Pause
  entirely while your view is backgrounded.
- **A `429` with no error envelope is not a rate limit — it is
  infrastructure.** Every deliberate `429` from this API carries the
  standard envelope with `RATE_LIMIT_EXCEEDED` or `QUOTA_EXCEEDED` (see the
  error table below) and a meaningful `Retry-After`. A bare `429`/`502`/`503`
  with **no** JSON envelope never reached the application at all — it came
  from the platform edge in front of it, typically while the instance was
  cold-starting. Branch on the envelope's presence: retry the envelope-less
  ones with exponential backoff, and surface the enveloped ones to the user
  as the deliberate answers they are. This distinction cost a real debugging
  session on 2026-08-27, when a bare edge `429` was read as the API's own
  rate limiter and searched for in application logs it was never in.

## 4. Handling `PARTIAL_SUCCESS`

This is a normal, expected outcome — three good angles and one failure is not
an error state. **Render what succeeded.** Don't discard the whole job because
one angle didn't come back. Each angle in the `angles` array carries its own
`status`; show the completed ones as product images and surface the failed
one(s) individually.

## 5. When to show a retry button

Show it **only when `retryable: true`** on that specific angle — never
globally, never based on parent job status. `retryable` is `false` for
`REJECTED` angles (safety refusals and QA rejections): these are
deterministic outcomes, retrying wastes money and will produce the identical
refusal. When `retryable` is `true`, `retry_url` gives you the exact endpoint
to call.

## 6. Displaying synthetic angles

Every angle status carries a `synthetic` boolean. When `true`, the image was
generated from a reference matrix with **no source photograph** — the model
is inventing a view it has never seen. **This must be visually flagged to the
end user** (a badge, watermark, or label is fine) so nobody mistakes a
generated angle for a photograph of the actual physical piece.

## 7. Signed URL expiry

`image_url` is a **freshly signed URL with a 1-hour TTL**, generated at the
moment you call `GET /status`. **Cache the image bytes, not the URL** — if you
need the image again after the TTL, re-poll `/status` for a fresh URL rather
than storing the URL itself.

Note on expired URLs: attempting to fetch a signed URL after its TTL expires
returns Supabase's own error response (currently `400`, not `403` — a
provider-behavior detail worth knowing if you're asserting on status codes in
your own tests, though normally you'll just re-poll before this happens).

## 8. Error codes and recommended UI treatment

| Code | HTTP | Meaning | Suggested UI treatment |
| :--- | :--- | :--- | :--- |
| `INVALID_API_KEY` | 401 | Bad/missing/revoked key | Configuration error — don't show to end user |
| `INSUFFICIENT_SCOPE` | 403 | Key lacks required scope | Configuration error |
| `CATEGORY_NOT_FOUND` | 422 | Unknown `category_code` | Form validation error |
| `CATEGORY_INACTIVE` | 422 | Category disabled in config | Form validation error |
| `ANGLE_NOT_ENABLED` | 422 | Angle not available for this category | Form validation error, hide that angle option |
| `SYNTHETIC_NOT_ALLOWED` | 422 | Synthetic requested where not permitted | Form validation error |
| `NO_ANGLES_REQUESTED` | 422 | All angles skipped | Form validation error — require at least one |
| `ASSET_NOT_FOUND` | 422 | Quoted `storage_path` doesn't exist | Re-upload and retry the request |
| `ASSET_NOT_OWNED` | 422 | `storage_path` belongs to another client | Configuration error |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | Missing `Idempotency-Key` header | Client bug — always send one |
| `IDEMPOTENCY_KEY_CONFLICT` | 409 | Same key, different payload | Client bug — generate a fresh key per distinct request |
| `JOB_NOT_FOUND` | 404 | Job doesn't exist or isn't yours | "Job not found" — don't distinguish from cross-tenant access |
| `SUBJOB_NOT_RETRYABLE` | 409 | Angle isn't `FAILED` | Hide/disable the retry button; refresh status |
| `RETRY_LIMIT_EXCEEDED` | 409 | 3 retries already used | Show a permanent failure state, no more retries |
| `INPUT_ASSET_EXPIRED` | 409 | Source photo past the 90-day retention window | Prompt to submit a brand-new job |
| `OPERATION_DISABLED` | 422 | Background operation not enabled in config | Form validation error — hide that operation option |
| `PRESET_NOT_FOUND` | 422 | Unknown `preset_code` | Form validation error — refresh `GET /config`'s preset list |
| `PRESET_INACTIVE` | 422 | `preset_code` exists but is no longer active | Form validation error — refresh the preset list |
| `ANGLE_JOB_RETRY_NOT_ALLOWED` | 409 | Called `/jobs/{job_id}/retry` on an angle job | Client bug — use `/jobs/{job_id}/angles/{angle}/retry` instead |
| `RATE_LIMIT_EXCEEDED` | 429 | Too many requests | Back off and retry after the `Retry-After` header |
| `QUOTA_EXCEEDED` | 429 | Daily job quota hit | Show a quota-exhausted message, don't auto-retry |
| `CONFIG_UNAVAILABLE` | 503 | No active config version | Transient — retry with backoff |
| `VALIDATION_ERROR` | 422 | Malformed request body | Form validation error; `details.errors` has field-level info |
| `INTERNAL_ERROR` | 500 | Unhandled server error | Generic "something went wrong," safe to retry once |

`code` is the stable contract — branch on it, never on `message`. `message` is
safe to show a user as-is if you have no better copy, but it may change
wording over time without notice.

## 9. Seeded demo scenarios (local/dev only)

These 8 jobs are pre-loaded by `scripts/seed_dev.py` and reachable with the
key above. Use them to build every UI state without needing a live Gemini
call.

| `job_id` | Scenario | Parent `status` | What to render |
| :--- | :--- | :--- | :--- |
| `616451d0-9451-4563-8b0f-60697bee0234` | 4/4 succeeded | `COMPLETED` | Four images |
| `b682df8b-6ccb-4738-a73c-335aef128e48` | 3 ok, 1 `FAILED` | `PARTIAL_SUCCESS` | Three images + retry button on the failed slot |
| `fc58410e-ecd0-4427-a336-78f877213668` | 3 ok, 1 `REJECTED` | `PARTIAL_SUCCESS` | Three images + **no** retry button, refusal message |
| `6e0d97be-99f7-499b-9506-d7dad0b94836` | 0/4 succeeded | `FAILED` | Error state, retry offered per angle |
| `f381a410-7d16-4efa-bafd-9da13b803c06` | 1 angle, failed | `FAILED` | Not partial — single-angle failure is total failure |
| `507c2dc2-7aab-4b66-95c5-21da1d348c74` | 2 requested, 2 skipped, both ok | `COMPLETED` | Two images, two empty slots |
| `6fddf78f-b8be-4dee-8116-42f0b26e21ea` | Synthetic in `QA_REVIEW` | `PROCESSING` | Still polling — no image yet |
| `aa118026-9c7d-4cbe-af6d-fc4b603366e0` | 2 done, 2 in flight | `PROCESSING` | Progressive render |

```
curl -H "X-API-Key: O1_pSgS_CjNAJOyFlexFwSaootlsX-kM8t9bJsAup_4" \
  https://<host>/api/v2/status/616451d0-9451-4563-8b0f-60697bee0234
```

`POST /generate` and `POST /jobs/{job_id}/angles/{angle}/retry` are mock in
this phase (`MOCK_MODE=true`): `/generate` hands back one of the jobs above
rather than creating a new one (Redis-backed idempotency replay still works
for real), and `/retry` checks real preconditions against the seeded angle's
state and returns `202`/`409` accordingly without mutating anything. Real job
creation and retry execution land in Phases 2 and 8.

## 10. Where this is hosted

No standing deployment exists yet — this phase ships the contract, the mock
server, and this guide, run locally against the real Supabase project
(Postgres, Storage) described in `docs/schema.md`. **Open item before
sign-off is complete:** deploy this mock server somewhere the Flutter team
can reach it over the network (a small always-on host, or a tunnel for the
sign-off session) and hand over that base URL alongside the key above.

---

## Sign-off checklist (for the sign-off session itself)

- [ ] Client and Flutter lead have walked through `PARTIAL_SUCCESS` (§4) and
      confirmed it's buildable
- [ ] Retryable/non-retryable distinction (§5) is understood
- [ ] Synthetic-angle visual flagging (§6) is confirmed as a requirement the
      ERP will implement
- [ ] Flutter team has successfully called `/config`, `/uploads/presign`,
      `/generate`, and `/status` against a reachable deployment
- [ ] Both confirmations are recorded in writing (this file isn't that
      record — capture it wherever the team tracks decisions)
