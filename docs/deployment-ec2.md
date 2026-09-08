# EC2 Deployment (Render Independence)

See `docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`
for the full design and rationale. This doc is the quick reference for
running this service on the shared EC2 instance instead of Render.

Render (`docs/deployment-free-tier.md`) and Fly (`docs/deployment.md`) stay
documented as fallback paths — this doc doesn't replace them.

## What runs

Four long-running containers plus one one-shot migration container, brought
up together:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

`migrate` itself waits for `redis` to start before it runs. `migrate` runs
`alembic upgrade head` once and exits; `api`, `worker`, and `beat` wait for
`migrate` and then run indefinitely with `restart: unless-stopped`.

## Env vars — deltas from the Render dashboard

**Updated 2026-09-08 — this list grew.** The database and object storage
move too now (see the design spec's Non-goals reversal note), so this is no
longer just a Redis-and-concurrency delta:

| Variable | Render value | EC2 value |
| :--- | :--- | :--- |
| `REDIS_URL` | Render Key Value instance URL | `redis://redis:6379/0` |
| `CELERY_BROKER_URL` | same instance, db 1 | `redis://redis:6379/1` |
| `CELERY_RESULT_BACKEND` | same instance, db 2 | `redis://redis:6379/2` |
| `IO_QUEUE_CONCURRENCY` | unset | irrelevant — `docker-compose.prod.yml` hardcodes `-c 2` on the worker command, this env var is not read in that path |
| `DATABASE_URL` | Supabase session pooler | Client's RDS instance — **URL-encode every special character**, `asyncpg` fails silently-then-loudly on a raw `%`/`#`/`£`/etc in the password. Real value from `docs/ec2-cutover-runbook.md` §2. |
| `S3_REGION` / `BUCKET_INPUTS` / `BUCKET_OUTPUTS` | (Supabase Storage was still active pre-cutover, S3 vars were the dormant post-Stage-A defaults) | Real bucket name(s) from the client, real region. See runbook §3. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | not set (Render has no instance profile, storage was still Supabase) | The client-issued S3 key. **Interim only** — Stage C's original plan was an EC2 instance profile instead of long-lived keys; that hasn't changed, this is what's available now. |

Everything else (`GEMINI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`,
`CONFIG_SHEET_ID`, `QA_*`, `MASK_*`, `WORKING_MAX_EDGE`, `APP_ENV`,
`MOCK_MODE`, `CONFIG_SYNC_CRON`, `SENTRY_DSN`) carries across verbatim from
Render — see `.env.example` for the full list.

**One thing that does NOT carry across:** don't copy Render's old
`SUPABASE_URL`/`SUPABASE_SERVICE_KEY` values even if they're still sitting
in the Render dashboard — V2's storage code has had no Supabase fallback at
all since the Stage A merge (`app/config.py` doesn't even define those
fields anymore). Leaving them out of `.env` is correct, not an oversight.

## Deploying an update

```bash
cd /opt/jewelry/jewelry-api
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`migrate` re-runs on every `up`; `alembic upgrade head` is a no-op when
already current.

## Rolling back to Render

**Updated 2026-09-08 — this is no longer a clean rollback for V2.** Render
was never deleted — only suspended — and resuming it still works
mechanically. But since the database and storage moved to RDS/S3 as part of
this same cutover, any job created *after* the RDS/S3 switch only exists
there — resuming Render would mean resuming a service still pointed at the
old Supabase project, which won't see that data. Rollback is clean only
until the point RDS/S3 goes live; after that, going back to Render means
either accepting the gap or re-pointing Render's own env vars at RDS/S3 too
(which defeats the purpose of rolling back). Decide the real rollback
window before suspending Render, not after.
