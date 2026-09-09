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

## nginx (added 2026-09-09)

The client's security group exposes **port 80 only** — they declined to open
8000/8001 ("We cannot directly expose the application on 0.0.0.0:<port>"), so
nginx fronts both services on one port and routes by path:

| Path | Upstream | Service |
| :--- | :--- | :--- |
| `/api/v2/`, `/ui` | `127.0.0.1:8000` | jewelry-api (V2) |
| `/api/v1/`, `/health` | `127.0.0.1:8001` | jewellery-gen-backend (V1) |
| `/s3-proxy/` | `image-enhancement-s3bucket.s3.amazonaws.com` | S3 upload passthrough |

The config is version-controlled at `deploy/nginx/jewelry.conf`. Install it:

```bash
sudo cp deploy/nginx/jewelry.conf /etc/nginx/sites-available/jewelry.conf
sudo ln -sf /etc/nginx/sites-available/jewelry.conf /etc/nginx/sites-enabled/jewelry.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl enable nginx   # must survive a reboot
```

**`/s3-proxy/` exists because the bucket has no CORS policy** and the client's
IAM user is denied `s3:PutBucketCORS`. Browsers block a direct PUT to S3; the
same request through this API's own origin is not cross-origin, so CORS never
applies. It pairs with `S3_UPLOAD_PROXY_BASE` — set one without the other and
uploads 404. Non-browser clients (including the production mobile ERP) are
unaffected either way. See
`docs/superpowers/plans/2026-09-09-s3-upload-proxy.md`.

**The bucket host is hardcoded in `deploy/nginx/jewelry.conf` and must be kept
in sync with the app's config by hand.** `proxy_pass` and the `Host` header
in the `/s3-proxy/` block both hardcode `image-enhancement-s3bucket.s3.amazonaws.com`,
which must always equal the host of the presigned URLs the app actually
generates for `settings.BUCKET_INPUTS` (`app/config.py`). There is no
automated link between them — a static nginx file can't be templated from
Python config without a deploy step. If the app's bucket setting or region
changes, or botocore's endpoint resolution behavior changes, and this file
isn't updated to match, uploads will start failing with 403s (signature
mismatch) or — if the hostnames happen to both resolve, just to different
buckets — silently land in the wrong bucket. Whoever changes `BUCKET_INPUTS`
must also update this file.

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
