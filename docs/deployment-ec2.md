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

`migrate` runs `alembic upgrade head` once and exits; `api`, `worker`, `beat`,
and `redis` wait for it and then run indefinitely with `restart:
unless-stopped`.

## Env vars — deltas from the Render dashboard only

Copy every value from Render's Environment tab for this service into this
host's `.env` unchanged, **except**:

| Variable | Render value | EC2 value |
| :--- | :--- | :--- |
| `REDIS_URL` | Render Key Value instance URL | `redis://redis:6379/0` |
| `CELERY_BROKER_URL` | same instance, db 1 | `redis://redis:6379/1` |
| `CELERY_RESULT_BACKEND` | same instance, db 2 | `redis://redis:6379/2` |
| `IO_QUEUE_CONCURRENCY` | unset | irrelevant — `docker-compose.prod.yml` hardcodes `-c 2` on the worker command, this env var is not read in that path |

Everything else (`DATABASE_URL`, `GEMINI_API_KEY`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `CONFIG_SHEET_ID`, `S3_REGION`,
`BUCKET_INPUTS`, `BUCKET_OUTPUTS`, `QA_*`, `MASK_*`, `WORKING_MAX_EDGE`,
`APP_ENV`, `MOCK_MODE`, `CONFIG_SYNC_CRON`, `SENTRY_DSN`) carries across
verbatim — see `.env.example` for the full list.

## Deploying an update

```bash
cd /opt/jewelry/jewelry-api
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`migrate` re-runs on every `up`; `alembic upgrade head` is a no-op when
already current.

## Rolling back to Render

Render was never deleted — only suspended. Resume the service from the
Render dashboard and repoint whatever calls this API back at the Render URL.
No data migration is needed in either direction since the database and
object storage never moved.
