# Deployment — Fly.io (requires a card)

Phase 12. See `phases/phase-12-cicd-deployment.md` for the full reality
check — the short version: this document and the config files it describes
are real and deployable, but nothing in this repo has been deployed live.
Every step below that needs a Fly.io or Upstash account is the user's to
run; this session has neither account and cannot create them.

**Fly.io requires a credit card on file for every account since a 2024
policy change (anti-abuse), even to stay within its free allowance.** If
that's not acceptable, use `docs/deployment-free-tier.md` instead — a
genuinely cardless path on Render, the same platform V1
(`jewellery-gen-backend`) already runs on in production. Both paths are
kept side by side rather than picking one, mirroring V1's own
`docs/deployment.md` (Railway) / `docs/deployment-free-tier.md` (Render)
split.

---

## Target

**Fly.io** for the API + Celery worker + Celery beat (three process groups
in one Fly app — `fly.toml` for production, `fly.staging.toml` for
staging). **Upstash** for Redis (broker, result backend, rate-limit/
idempotency counters). **Supabase** Postgres, unchanged from every prior
phase.

Chosen over Render (free tier doesn't cover background workers — Celery
needs one, though `docs/deployment-free-tier.md` now solves that
differently) and Railway (one-time free credit, not an ongoing free tier) —
Fly's small shared-CPU machine allowance and Upstash's serverless Redis
free tier are both usage-free on an ongoing basis at this project's likely
volume (roadmap open decision #4 is still unresolved on exact volume —
revisit if usage outgrows the free tier), but Fly's card requirement is a
real cost of entry `docs/deployment-free-tier.md` avoids entirely.

---

## One-time setup (user-only — this session cannot do any of this)

1. Create a Fly.io account. `fly apps create jewelry-api` and
   `fly apps create jewelry-api-staging`.
2. Create an Upstash account. **Correction: a free Upstash account allows
   only one free database** (found by the user actually signing up, not
   assumed here) — a paid Upstash plan is required to get a genuinely
   separate database for staging vs. production; on the free tier, both
   environments share the one instance, which does mean a staging bug
   could touch production's idempotency keys or rate-limit counters. If
   that risk isn't acceptable, either pay for a second Upstash database or
   drop the staging environment entirely on this path — sharing one Redis
   between environments while keeping it silently as a `docs/deployment.md`
   default would be worse.
3. `fly tokens create deploy` → add the result as the `FLY_API_TOKEN`
   secret in the GitHub repo (Settings → Secrets and variables → Actions).
4. Set every secret below on **both** Fly apps (`fly secrets set NAME=value
   -a jewelry-api` and again `-a jewelry-api-staging` with
   environment-appropriate values — staging should point at its own
   Supabase project if one exists, or a separate schema/prefix at minimum,
   never production's Postgres).

## Secrets checklist

Cross-checked directly against `app/config.py::Settings` — every field
listed here, in the same order, so this list can't silently drift from
what the app actually reads. `(required)` = no usable default;
`(has a default, but production needs a real value)` = technically
optional to Pydantic, functionally required for the app to do anything
useful in production.

| Var | Status |
| :--- | :--- |
| `APP_ENV` | Set via `fly.toml`/`fly.staging.toml`'s `[env]`, not a secret |
| `LOG_LEVEL` | Default `INFO` is fine |
| `API_BASE_PATH` | Default `/api/v2` is fine |
| `MOCK_MODE` | Set via `[env]`, must be `false` — both `fly.toml` files already set this |
| `DATABASE_URL` | **(required)** — Supabase session pooler URL, port 5432, not the transaction pooler. **URL-encode any special character in the password** (`@` → `%40`, `#` → `%23`, `/` → `%2F`, `:` → `%3A`). A raw `@` makes SQLAlchemy split the URL at the wrong `@`, so the host parses as `<tail-of-password>@aws-0-….pooler.supabase.com` and the deploy dies at `alembic upgrade head` with `socket.gaierror: [Errno -2] Name or service not known` — which reads like a DNS/network outage but is purely a parsing bug. Copy this value from a known-good `.env`; never retype it by hand. |
| `SUPABASE_URL` | (has a default, but production needs a real value) |
| `SUPABASE_SERVICE_KEY` | (has a default, but production needs a real value) — never log this, `docs/conventions.md` |
| `BUCKET_INPUTS` | Default `jewelry-inputs` is fine unless bucket names differ per environment |
| `BUCKET_OUTPUTS` | Default `jewelry-outputs` is fine, same caveat |
| `SIGNED_URL_TTL_SECONDS` | Default `3600` is fine |
| `STORAGE_MAX_ATTEMPTS` | Default `3` is fine to start — bounds `storage_service._with_retries`, which retries only `httpx.TransportError` (never a real Supabase error response); see `app/services/storage_service.py`'s own module docstring on the CI/production flake this fixes |
| `STORAGE_RETRY_BACKOFF_SECONDS` | Default `0.5` is fine to start — linear backoff multiplier between retried Storage calls |
| `RETENTION_SWEEP_CRON` | Default is fine |
| `WORKER_TASK_TIMEOUT_SECONDS` | Default `180` is fine — bounds a hung generation/background task via `asyncio.wait_for`, the mechanism that actually enforces this under `--pool=solo` (Phase 16 Step 1; Celery's own `task_time_limit`/`task_soft_time_limit`, set in `app/workers/celery_app.py`, are inert under solo — see that file's comment) |
| `RECONCILIATION_SWEEP_CRON` | Default `*/15 * * * *` is fine — deliberately frequent; a stuck job is a client-visible symptom (Phase 16 Step 2) |
| `RECONCILIATION_STALE_AFTER_SECONDS` | Default `600` is fine — should stay comfortably above `WORKER_TASK_TIMEOUT_SECONDS` |
| `REDIS_URL` | (has a default, but production needs the real Upstash `rediss://` URL) |
| `CELERY_BROKER_URL` | Same — Upstash `rediss://` URL. Celery refuses a `rediss://` URL with no `ssl_cert_reqs` (`ValueError: E_REDIS_SSL_CERT_REQS_MISSING_INVALID`, killing `celery beat` at startup); `app/workers/celery_app.py` now sets `CERT_REQUIRED` automatically for any `rediss://` URL, so no query param is needed. An explicit `?ssl_cert_reqs=required` is equivalent and harmless. |
| `CELERY_RESULT_BACKEND` | Same — Upstash `rediss://` URL, same TLS note as the broker |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | Default `5` is fine — bounds a silently stalled Redis connection so it can't hang a worker forever (see `app/core/redis_client.py`) |
| `IO_QUEUE_CONCURRENCY` | Default `20` is fine to start |
| `QA_MODEL_ID` | Default empty is fine — not read by any code path yet (unused since Phase 9 decided the QA judge is Gemini, not a separate embedding model) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | (has a default, but Sheets sync needs a real value — see roadmap open decision #2, still open) |
| `CONFIG_SHEET_ID` | Same caveat |
| `CONFIG_SYNC_CRON` | Default is fine |
| `GEMINI_API_KEY` | (has a default, but production needs a real value — no real key has existed in any environment through Phase 9) |
| `GEMINI_RATE_LIMIT_PER_MINUTE` | Default `60` is fine to start |
| `GEMINI_REQUEST_TIMEOUT_SECONDS` | Default `120` is fine to start — bounds a hung/slow Gemini call so it can't block the whole worker indefinitely (see `app/providers/gemini.py`) |
| `MASK_ERODE_PX` | Default `2` is fine to start — RECOLOR mask erosion, see `app/services/recolor_service.py` |
| `MASK_FEATHER_PX` | Default `3` is fine to start — RECOLOR compositing feather, see `app/services/recolor_service.py` |
| `MASK_MIN_COVERAGE_PCT` | Default `0.5` is fine to start — RECOLOR mask contract, see `app/services/mask_validation.py` |
| `MASK_MAX_COVERAGE_PCT` | Default `60.0` is fine to start — RECOLOR mask contract, see `app/services/mask_validation.py` |
| `WORKING_MAX_EDGE` | Default `2048` is fine to start — caps RECOLOR/MIX pre-provider-call working resolution; see `app/config.py`'s own note on the live 2026-08-24 OOM this fixes |
| `SENTRY_DSN` | (has a default; empty means no error reporting — fine to leave empty until Phase 11 is built) |

`rediss://` (TLS) is Upstash's URL scheme — `redis.asyncio.from_url`
already handles it (confirmed by reading `redis-py`'s URL parsing, not
tested live — no Upstash instance exists to test against in this session).

---

## Deploy flow

- Push to `main` (after CI passes) → `deploy-staging` job
  (`.github/workflows/deploy.yml`) runs `flyctl deploy --config
  fly.staging.toml`.
- Push a tag matching `v*` (after CI passes on that tag) →
  `deploy-production` job runs `flyctl deploy --config fly.toml`.
  Deliberately not automatic on every `main` push — cutting a tag is a
  human decision to ship.
- Both gate on the `CI` workflow's success via `workflow_run` — a red CI
  run can never trigger either deploy.
- `[deploy] release_command = "alembic upgrade head"` in both `fly.toml`
  files runs the migration against the new release before traffic shifts
  to it; a failed migration fails the whole deploy, old release stays live.

## Rollback

Fly's own release history is the rollback mechanism — no custom tooling
built for this, since one already exists:

```bash
fly releases -a jewelry-api            # list past releases, find the image ref to roll back to
fly deploy --image <previous-image-ref> -a jewelry-api
```

Same commands with `-a jewelry-api-staging` for staging. A rollback does
**not** run `release_command` again — if the incident was caused by a
migration, the migration itself needs a forward-fix migration, not a
rollback (`docs/conventions.md`: "Migrations are forward-only in
production").
