# Deployment — AWS App Runner

Phase 17. See `docs/decisions/0003-deploy-to-aws.md` for why App Runner over ECS
Fargate or EC2. Joins `docs/deployment.md` (Fly.io, config-only, never deployed) and
`docs/deployment-free-tier.md` (Render, live in production through Phase 16) — this
path replaces Render as primary once verified live; neither prior doc is deleted, same
precedent Phase 12's own addendum set when Render was added alongside Fly.

**The container and `scripts/render_start.sh` are unchanged.** App Runner runs the
same image, unmodified — that's the entire reason this platform was chosen over ECS or
EC2. See `docs/decisions/0003-deploy-to-aws.md`.

---

## Pipeline

CI (`.github/workflows/ci.yml`) → `.github/workflows/deploy-aws.yml`, gated via
`workflow_run` on CI's success — the same pattern `deploy.yml`'s Fly jobs already use,
so a red CI run can never reach a deploy. A push to `main` builds and pushes a
staging-tagged image to ECR; a pushed `v*` tag builds and pushes the production image
and explicitly triggers an App Runner deployment. Same "cutting a tag is a deliberate
human decision to ship" reasoning `deploy.yml`/`docs/deployment.md` already use.

**Authentication is OIDC** (`aws-actions/configure-aws-credentials`, `role-to-assume`),
not a long-lived AWS access key pair committed as a secret — a real improvement over
the Fly path's static `FLY_API_TOKEN`. Needs a GitHub OIDC identity provider and an IAM
role with a trust policy scoped to this repo, set up in the AWS account (see "What only
the user can do" below).

## Service configuration

| | |
| :--- | :--- |
| **Source** | Amazon ECR image (`jewelry-api` repo for production, `jewelry-api-staging` for staging) — not App Runner's source-connected build. CI already builds and tests the image; App Runner deploying a known-good, already-tagged image is more consistent with how both other deploy paths are already gated than letting App Runner build from source itself. |
| **Port** | `8000` — matches `uvicorn`'s bind in `scripts/render_start.sh`, unchanged. |
| **Health check** | `GET /api/v2/health` — same endpoint every deploy path uses. Already checks Postgres and Redis reachability (`app/api/v2/health.py`), so App Runner's health check reuses real application logic instead of duplicating it — same reasoning `docs/deployment.md` gives for Fly. |
| **Instance size** | 1 vCPU / 2 GB to start — deliberately more headroom than Render's free-tier 512MB. Not compensating for an unfixed bug (Phase 16 already fixed the one that caused the OOM crash-loop); it's realistic headroom for a paid tier where the cost gap to the next size up is small next to the cost of another OOM incident on a client-facing system. |
| **Auto scaling** | Min 1 instance — this is not a scale-to-zero workload; Celery beat and the worker need to be continuously present, not request-triggered, same as every other deploy path here. Max left at App Runner's default until real traffic data exists (`phases/phase-13-load-soak-tuning.md`). |
| **Auto-deploy** | `AutoDeploymentsEnabled` on the ECR repository, so a new image push alone triggers a deployment — `deploy-aws.yml`'s explicit `aws apprunner start-deployment` call for the production job is belt-and-braces on top of this, not the only trigger, and is idempotent against an image tag App Runner is already deploying. |

## Secrets

Cross-checked against `app/config.py::Settings` field-by-field — every field it
declares appears below, same order, same mechanism `docs/deployment.md`'s own table
uses (`tests/unit/test_deployment_docs.py` enforces this for that file; extend that
test to cover this one too if this path becomes primary — not done yet since App
Runner doesn't exist to verify against).

| Var | AWS Secrets Manager? | Note |
| :--- | :--- | :--- |
| `APP_ENV` | No — App Runner env var | `production` |
| `LOG_LEVEL` | No | Default `INFO` fine |
| `API_BASE_PATH` | No | Default `/api/v2` fine |
| `MOCK_MODE` | No | Must be `false` |
| `DATABASE_URL` | **Yes** | Same Supabase session-pooler URL already in use. Same URL-encoding warning `docs/deployment.md` already documents — a raw `@`/`#`/`/`/`:` in the password breaks the URL parse the same way regardless of platform |
| `SUPABASE_URL` | **Yes** | |
| `SUPABASE_SERVICE_KEY` | **Yes** | Never logged — `docs/conventions.md` |
| `BUCKET_INPUTS` | No | Default `jewelry-inputs` fine |
| `BUCKET_OUTPUTS` | No | Default `jewelry-outputs` fine |
| `SIGNED_URL_TTL_SECONDS` | No | Default `3600` fine |
| `STORAGE_MAX_ATTEMPTS` | No | New 2026-08-28 — default `3` fine. Bounds `storage_service._with_retries`, added after the identical transient-timeout signature failed CI five times in one week |
| `STORAGE_RETRY_BACKOFF_SECONDS` | No | New 2026-08-28 — default `0.5` fine |
| `RETENTION_SWEEP_CRON` | No | Default fine |
| `RECONCILIATION_SWEEP_CRON` | No | New in Phase 16 — default `*/15 * * * *` fine. Chosen partly because Render's free tier restarts every 1-4 hours and can miss a daily cron entirely; App Runner's always-on instance doesn't have that problem, but there's no reason to make the sweep less frequent just because the platform changed — a stuck job is still a client-visible symptom |
| `RECONCILIATION_STALE_AFTER_SECONDS` | No | New in Phase 16 — default `600` fine |
| `REDIS_URL` | **Yes** | Same Upstash `rediss://` URL, unchanged |
| `CELERY_BROKER_URL` | **Yes** | Same Upstash `rediss://` URL. `app/workers/celery_app.py` sets `CERT_REQUIRED` automatically for any `rediss://` URL — no extra query param needed, same as both other paths |
| `CELERY_RESULT_BACKEND` | **Yes** | Same Upstash `rediss://` URL |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | No | Default `5` fine |
| `IO_QUEUE_CONCURRENCY` | No | **Not read by `render_start.sh`'s `--pool=solo` invocation** — same "declared but inert on this path" caveat `render.yaml` already carries (see `app/workers/celery_app.py`'s Phase 16 comment on why `task_time_limit`/`task_soft_time_limit` are also inert under solo). App Runner's larger instance size makes reintroducing prefork a reasonable follow-up, but that's a deliberate decision for whoever owns capacity tuning next, not a default to flip silently in this phase |
| `WORKER_TASK_TIMEOUT_SECONDS` | No | New in Phase 16 — default `180` fine. This is what actually bounds a hung task now, via `asyncio.wait_for` — carries forward unchanged regardless of platform, since it's an application-level mechanism, not a Celery-pool one |
| `QA_MODEL_ID` | No | **Now read** (2026-08-30) — empty falls back to the config's image-generation `model_version`; set a text-capable model to stop judge-call 503s. See `docs/deployment.md` |
| `QA_MAX_ATTEMPTS` | No | Default `3` — bounded retry around the judge call |
| `QA_RETRY_BACKOFF_SECONDS` | No | Default `0.5`, linear |
| `QA_PASS_ON_PROVIDER_ERROR` | No | Default `true` — unevaluated outputs complete rather than flag; see `docs/business-rules.md` §7 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **Yes** | Still needed for Sheets sync — roadmap open decision #2, still open |
| `CONFIG_SHEET_ID` | No | Not secret, but environment-specific |
| `CONFIG_SYNC_CRON` | No | Confirm the `0 * * * *` override that fixed the 2026-08-12 beat-schedule/OOM collision (documented in `render.yaml`) carries forward here too, rather than reverting to the `*/15 * * * *` code default |
| `GEMINI_API_KEY` | **Yes** | |
| `GEMINI_RATE_LIMIT_PER_MINUTE` | No | Default `60` fine to start |
| `GEMINI_REQUEST_TIMEOUT_SECONDS` | No | Default `120` fine |
| `MASK_ERODE_PX` | No | Default `2` fine — RECOLOR mask erosion |
| `MASK_FEATHER_PX` | No | Default `3` fine — RECOLOR compositing feather |
| `MASK_MIN_COVERAGE_PCT` | No | Default `0.5` fine — RECOLOR mask contract |
| `MASK_MAX_COVERAGE_PCT` | No | Default `60.0` fine — RECOLOR mask contract |
| `WORKING_MAX_EDGE` | No | Default `2048` fine — caps RECOLOR/MIX pre-provider-call working resolution, see `app/config.py`'s note on the 2026-08-24 OOM this fixes |
| `SENTRY_DSN` | **Yes**, if set | Empty is valid (no error reporting) until Phase 11 is built |

Secrets referenced via App Runner's native Secrets Manager integration
(`--secrets` / the `RuntimeEnvironmentSecrets` field in the service configuration),
resolved at container start — never baked into the image or printed in CI logs.

## Migrations

App Runner has no first-class release-command hook the way Fly's `[deploy]
release_command` does. `scripts/render_start.sh` already runs `alembic upgrade head`
before starting the worker/uvicorn processes, unconditionally — that carries forward to
App Runner with **zero modification**, another point in App Runner's favor per
`docs/decisions/0003-deploy-to-aws.md`.

## Rollback

App Runner keeps a deployment history per service, but the image tag is the actual
source of truth here (deploys are commit-SHA-tagged, per `deploy-aws.yml`) — treat
rolling the service's configured image tag back to a known-good SHA as the primary
rollback path, not App Runner's deployment-history UI:

```bash
# List recent operations (deployments, rollbacks) for the service:
aws apprunner list-operations --service-arn <service-arn>

# Point the service at a known-good image tag (update-service with the new
# ImageIdentifier), then redeploy:
aws apprunner update-service \
  --service-arn <service-arn> \
  --source-configuration '{
    "ImageRepository": {
      "ImageIdentifier": "<ecr-registry>/jewelry-api:<known-good-sha>",
      "ImageRepositoryType": "ECR"
    },
    "AutoDeploymentsEnabled": true
  }'

aws apprunner start-deployment --service-arn <service-arn>
```

**A rollback does not re-run `alembic upgrade head`** — same note `docs/deployment.md`
already carries for Fly. If the incident was caused by a migration, the fix is a
forward migration, not a rollback (`docs/conventions.md`: "migrations are forward-only
in production").

## What only the user can do

This session can produce correct, deployable configuration and a real CI→CD pipeline
definition. It cannot create an AWS account, an ECR repository, an App Runner service,
or IAM/OIDC credentials on the user's behalf — same honesty split
`docs/deployment.md` already uses for the Fly path, which was never account-tested.

Before either `deploy-aws.yml` job can succeed for real, the user needs to:

1. Create (or use an existing) AWS account.
2. Create two ECR repositories: `jewelry-api` (production) and `jewelry-api-staging`.
3. Create a GitHub OIDC identity provider in IAM (`token.actions.githubusercontent.com`),
   if one doesn't already exist for this AWS account.
4. Create two IAM roles (staging, production) trusting that OIDC provider, scoped to
   this repo (`repo:animachiii/jewelry-api:*`), with permission to push to the
   matching ECR repository and (production only) call `apprunner:StartDeployment` on
   the production service.
5. Add repo secrets: `AWS_DEPLOY_ROLE_ARN_STAGING`, `AWS_DEPLOY_ROLE_ARN_PRODUCTION`,
   `AWS_APPRUNNER_SERVICE_ARN_PRODUCTION`.
6. Create the two App Runner services (staging, production) per the "Service
   configuration" table above, each pointed at its ECR repository with
   `AutoDeploymentsEnabled`.
7. Add every **Yes**-marked secret above to AWS Secrets Manager and reference each in
   the App Runner service's `RuntimeEnvironmentSecrets` configuration.
8. Push to `main` (staging) or a `v*` tag (production) and confirm a real deployment
   goes live, with `GET /api/v2/health` returning a real `200`.

Until step 8 happens for real, this phase's App-Runner-specific checkpoints stay
`👤` — a later, separate confirmation, same as Phase 12's Fly path never fully closed
and the Render path only closed once someone actually ran it.
