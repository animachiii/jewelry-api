# Phase 10 — Auth & Security Hardening

## Reality check before writing this

The roadmap's one-line description is broad: "Key rotation, per-client rate
limits and quotas, secrets management, input sanitization, URL scoping,
pen-test pass." Reading the actual codebase before touching anything:

- **Real auth already exists** (Phase 1): `X-API-Key` → Argon2 verify →
  `ApiClient`, `client`/`ops` scope enforcement on every route
  (`app/core/auth.py`). Nothing here needs rebuilding.
- **Per-client rate limiting is built but dead code.**
  `app/core/ratelimit.py::allow` — a Redis fixed-window counter keyed
  `ratelimit:{client_id}:{minute}` (already documented in `docs/schema.md`'s
  Redis table) — exists since Phase 0/1 and **nothing calls it**. Confirmed
  by grep, not assumed. `docs/api-routes.md` already documents the contract
  this is supposed to satisfy: "Returns `429` when the client's rate limit
  or daily quota is exceeded, with `Retry-After`." That response has never
  actually happened.
- **Daily quota is the same story.** `api_clients.daily_job_quota` (nullable
  = unlimited) exists in the schema since Phase 0 and is never read anywhere
  in `app/`.
- **`ErrorCode.RATE_LIMIT_EXCEEDED` and `QUOTA_EXCEEDED` already exist**
  (`app/core/errors.py`), and `RateLimitError` (429) already exists —
  scaffolded ahead of time, never wired to anything that raises it for a
  real reason. No `QuotaExceededError` class exists yet.
- **URL scoping is already correct**, not a gap: signed URLs are generated
  per-request with a 1-hour TTL (`storage_service.generate_signed_url`,
  Phase 1/4), `GET /status/{job_id}` already 404s (never 403s) another
  client's job, and asset bytes are never reachable except through a job a
  client owns. This phase adds a regression test confirming it, not new
  code — see Step 3.
- **Secrets management is already enforced by convention**, not absent:
  `docs/conventions.md` already mandates env-vars-only config and "never
  log raw API keys, `SUPABASE_SERVICE_KEY`, `GEMINI_API_KEY`... log the
  `key_prefix`... never the signed URL," and `app/core/auth.py` already only
  ever logs/stores `key_prefix` + an Argon2 hash, never a raw key. This
  phase adds a static test that enforces the logging rule mechanically
  (grep-based), not new runtime code — see Step 2.
- **Input sanitization is already substantially built**: Pydantic
  `extra="forbid"` schemas, `app/services/image_validation.py`'s real
  decode-and-validate step (Phase 4), category/angle/synthetic validation
  (Phase 2). Nothing new to add here beyond what Step 2's audit finds, if
  anything.

**What this phase actually builds**, matching the two items above that are
real gaps, not just prose:

1. Real per-client rate limiting **and** daily quota enforcement on
   `POST /generate` (the only route `docs/api-routes.md` documents `429`
   for) — 429 with `Retry-After`, matching the contract that's existed on
   paper since Phase 1.
2. A static "no secret leakage" audit test, made permanent rather than
   verified once by eye.
3. A URL-scoping regression test confirming what's already true.

**Explicitly not built this phase, and why — flagged rather than invented:**

- **Key rotation.** There is no route for it anywhere in
  `docs/api-routes.md` — not `client`-scope, not `ops`-scope. Building an
  endpoint here would mean inventing API surface the contract doesn't
  define: who can rotate whose key, whether the old key stays valid during
  a grace period, what happens to in-flight requests signed with the old
  key. That's a product/API design decision, not something this phase can
  resolve by reading code. Added as a new roadmap open decision (#9) rather
  than silently skipped or silently invented.
- **Pen-test pass.** Needs a human or an external tool running against a
  live deployment — there is no live deployment yet (Phase 1's own open
  item: "no standing deployment exists"). Not something a phase can
  self-audit into existence. Revisit once Phase 12 stands up a real
  environment.

---

## Step 1 — Real rate limiting and daily quota on `/generate`

### What to do

`app/core/errors.py`: add `QuotaExceededError(AppError)`,
`code = ErrorCode.QUOTA_EXCEEDED`, `http_status = 429`. Give both
`RateLimitError` and `QuotaExceededError` a `retry_after_seconds: int`
constructor param (default a sane value — 60 for rate limit, since the
window is per-minute; the seconds remaining until UTC midnight for quota).
`app/core/errors.py::_app_error_handler`: if the raised `AppError` carries a
`retry_after_seconds` attribute, set the `Retry-After` response header —
same header `GET /status` already sets for non-terminal jobs
(`app/api/v2/status.py`), reused for the same purpose here.

`app/db/repositories/jobs.py`: `count_created_today(session, client_id) ->
int` — counts `jobs` rows for this client with `created_at >=` the current
UTC day's start. Postgres-backed, not a new Redis key — `daily_job_quota`
has no corresponding entry in `docs/schema.md`'s Redis table and shouldn't
grow one; matches the "Postgres is the system of record" decision already
made for everything else client-visible.

`app/services/job_service.py::create_job_for_request`: immediately after
the existing idempotency-replay check (a replay must not consume a fresh
rate-limit token or quota slot — it doesn't create a job or bill the
provider, per `docs/business-rules.md` §8, so it shouldn't cost rate budget
either) and before category/angle validation:

1. `ratelimit.allow(str(client.id), client.rate_limit_per_min)` — `False`
   raises `RateLimitError(retry_after_seconds=60)`.
2. If `client.daily_job_quota is not None`:
   `jobs_repo.count_created_today(session, client.id) >=
   client.daily_job_quota` raises `QuotaExceededError` with
   `retry_after_seconds` computed to UTC midnight.

### Checkpoint 1

- [ ] A client whose `rate_limit_per_min` is exceeded gets `429
      RATE_LIMIT_EXCEEDED` with a `Retry-After` header — real Redis-backed
      counter, not simulated
- [ ] A client under the limit is unaffected — confirms this doesn't
      regress the existing happy-path `/generate` tests
- [ ] A client whose `daily_job_quota` is exceeded gets `429
      QUOTA_EXCEEDED` with `Retry-After`
- [ ] A client with `daily_job_quota: NULL` (unlimited) is never quota-
      blocked no matter how many jobs it creates
- [ ] An idempotent replay of an existing key does not consume a rate-limit
      token or count toward the daily quota

---

## Step 2 — Secret-leakage static audit

### What to do

New `tests/unit/test_secret_logging.py`: a static test (no server, no
fixtures) that greps `app/` source for any `logger.*(...)` call whose
arguments reference `SUPABASE_SERVICE_KEY`, `GEMINI_API_KEY`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `key_hash`, or a raw `x_api_key`/API key
variable by name, and fails if one is found — mechanizing the rule
`docs/conventions.md` already states in prose. Also asserts
`app/core/auth.py` never logs anything (it doesn't today — this pins that).

### Checkpoint 2

- [ ] The audit test passes against the current codebase as-is (no
      violations found — confirms the existing discipline, doesn't require
      fixing anything)
- [ ] A deliberately introduced violation (temporarily, in the test itself
      via a monkeypatched fake module, not a real code change) is caught by
      the test — proves the grep is actually looking for the right thing,
      not passing vacuously

---

## Step 3 — URL-scoping regression test

### What to do

New test in `tests/integration/test_api_contract.py` (or alongside it):
confirms a client cannot construct or guess a working signed URL for
another client's asset, and that `GET /status/{job_id}` for another
client's job is `404`, never `403` — the second part already has coverage
(`test_other_clients_job_id_returns_404_not_403`,
`tests/integration/test_mock_fixtures.py`); this step adds the asset-level
check that doesn't exist yet: a signed URL is bucket+path+expiry-scoped by
Supabase Storage itself, not by anything this codebase enforces
client-side, so the real assertion is that the API never *hands* client B a
signed URL to client A's asset — verified by confirming `GET
/status/{job_id}` 404s before any `image_url` could ever be read from the
response.

### Checkpoint 3

- [ ] Client B requesting client A's `job_id` gets `404` with no
      response body field that could leak a storage path or signed URL
- [ ] Existing `test_other_clients_job_id_returns_404_not_403` still passes
      unchanged — this step adds coverage, it doesn't replace anything

---

## Step 4 — Self-audit

Same discipline as every prior phase: re-read every checkpoint with real
tests (testcontainers Postgres, real local Redis — Step 1 needs a genuine
Redis counter, not fakeredis, to prove the fixed-window logic), fix
failures before declaring done. Sync `docs/api-routes.md` if the `429`
wording needs any adjustment (it shouldn't — this phase makes existing prose
true, not different), `CLAUDE.md`, `phases/phase-roadmap.md`. Add roadmap
open decision #9 (key rotation — no route defined in `docs/api-routes.md`).

---

## Note for later phases

Phase 12 (CI/CD & Deployment) is sequential after this one on the roadmap.
Nothing in this phase blocks it structurally, but a pen-test pass genuinely
can't happen until Phase 12 stands up a real environment — worth revisiting
whether that ordering should be "Phase 10 code hardening → Phase 12 deploy →
pen-test" as three separate gates rather than one phase's checkpoint, next
time the roadmap is revised.
