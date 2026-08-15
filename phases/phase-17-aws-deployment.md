# Phase 17 — AWS Deployment (App Runner)

## Reality check before writing this

Same category of phase as Phase 12: this session (or the Claude Code session executing this phase) can produce real, correct, deployable configuration and a real CI→CD pipeline definition — it cannot create an AWS account, an ECR repository, an App Runner service, or IAM credentials on the user's behalf. Every checkpoint below states plainly whether it's something provable from this session or something only the user can prove by actually running it, following the same honesty split Phase 12 used for Fly.io.

**Target, per `docs/decisions/0003-deploy-to-aws.md`:** AWS App Runner running the existing container unchanged, Amazon ECR for the image, Supabase Postgres and Upstash Redis unchanged, AWS Secrets Manager for secrets.

**Depends on Phase 16 completing.** Deploying to AWS before Phase 16's reconciliation sweep and task time limits land just moves the same unresolved stuck-job and unbounded-hang risk onto a platform that costs money per hour whether or not it's stuck.

---

## Step 1 — Container registry and build pipeline

### What to do

Create `.github/workflows/deploy-aws.yml`, `needs: ci` against the existing `ci.yml` workflow (same gate `deploy.yml`'s Fly jobs already use — a red CI run can never deploy):

- On push to `main` after CI passes: build the existing `Dockerfile` unchanged, tag with the commit SHA, push to a new ECR repository (`jewelry-api`), and trigger an App Runner deployment via `aws apprunner start-deployment` (or App Runner's auto-deploy-on-ECR-push setting, which removes the explicit trigger step — prefer this if the App Runner service is configured with `AutoDeploymentsEnabled`, since it needs one less credential in CI).
- Authenticate to AWS via OIDC (`aws-actions/configure-aws-credentials` with a GitHub OIDC role), not a long-lived access key pair committed as a repo secret — this is a real improvement over the Fly path, which used a static `FLY_API_TOKEN`.
- Mirror the tag-triggers-production pattern from `deploy.yml`: pushes to `main` deploy to a staging App Runner service, a pushed `v*` tag deploys to production. Same reasoning as Phase 12 — cutting a tag is a human decision to ship.

Do **not** create a second Dockerfile or modify `scripts/render_start.sh`. The entire reason App Runner was chosen in `0003` is that the existing container already runs correctly as a single deployable unit; a phase that ends up rewriting the container defeats that reasoning.

### Checkpoint 1

- [ ] `.github/workflows/deploy-aws.yml` is valid workflow YAML, its `needs`/`on` triggers reference real, existing job/workflow names — mechanically checkable without an AWS account
- [ ] The workflow authenticates via OIDC, not a static access key — checkable by reading the workflow file, no AWS account needed to verify this
- [ ] `docker build .` using the existing, unmodified `Dockerfile` succeeds and the resulting image's `CMD` is still `./scripts/render_start.sh` — provable locally, right now, without any AWS account
- [ ] **User-only:** a real push to `main` actually builds, pushes to a real ECR repository, and triggers a real App Runner deployment — needs an AWS account, an ECR repository, an App Runner service, and the OIDC role/trust policy configured, none of which this session can create

---

## Step 2 — App Runner service configuration

### What to do

Write `docs/deployment-aws.md` (new — joins `docs/deployment.md` and `docs/deployment-free-tier.md` rather than replacing either, same "keep prior paths" precedent Phase 12's addendum set) documenting the App Runner service configuration:

- **Source:** ECR image, not a GitHub-connected source — App Runner can build from source directly, but this project already has a working, tested `Dockerfile`; building through CI and pushing a known-good image is more consistent with how CI is already gating both other deploy paths.
- **Port:** `8000`, matching `uvicorn`'s bind in `scripts/render_start.sh`.
- **Health check:** `GET /api/v2/health`, same endpoint both other paths already use — it already checks Postgres and Redis reachability, so App Runner's health check reuses real application logic instead of duplicating it, same reasoning Phase 12 used for Fly.
- **Instance size:** start at App Runner's 1 vCPU / 2GB configuration — deliberately more headroom than Render's 512MB free tier, given `scripts/render_start.sh`'s own measured ~250MB idle + ~156MB per background-operation job profile. This is not meant to paper over a memory bug (Phase 16 already fixed the one that was found) — it's realistic headroom for a paid tier where the cost difference between the smallest and next-smallest instance size is small relative to the cost of another OOM incident on a client-facing system.
- **Auto scaling:** min 1 instance (matches "always running," same as Render and Fly's behavior — this is not a scale-to-zero workload, since Celery beat and the worker need to be continuously present, not requested), max sized once real traffic data exists — leave at App Runner's default max initially and revisit alongside `phases/phase-13-load-soak-tuning.md`'s eventual live-instance measurements.

**Secrets checklist**, cross-checked against `app/config.py::Settings` the same way `docs/deployment.md`'s table was built — every field, same order, so this can't silently drift from what the app actually reads:

| Var | AWS Secrets Manager? | Note |
| :--- | :--- | :--- |
| `APP_ENV` | No — App Runner environment variable | `production` |
| `LOG_LEVEL` | No | Default `INFO` fine |
| `API_BASE_PATH` | No | Default `/api/v2` fine |
| `MOCK_MODE` | No | Must be `false` |
| `DATABASE_URL` | **Yes** | Same Supabase session-pooler URL already in use; same URL-encoding warning `docs/deployment.md` already documents (`@`, `#`, `/`, `:` in the password) |
| `SUPABASE_URL` | **Yes** | |
| `SUPABASE_SERVICE_KEY` | **Yes** | Never logged — `docs/conventions.md` |
| `BUCKET_INPUTS` / `BUCKET_OUTPUTS` | No | Defaults fine |
| `SIGNED_URL_TTL_SECONDS` | No | Default fine |
| `RETENTION_SWEEP_CRON` | No | Default fine |
| `RECONCILIATION_SWEEP_CRON` | No | New in Phase 16 — default fine |
| `REDIS_URL` / `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | **Yes** | Same Upstash `rediss://` URLs already in use — unchanged from the Render path |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | No | Default fine |
| `IO_QUEUE_CONCURRENCY` | No | Not read by `render_start.sh`'s `--pool=solo` invocation — same "declared but inert on this path" caveat `render.yaml` already documents. If App Runner's larger instance size makes prefork worth reintroducing, that's a deliberate follow-up decision, not a default to flip silently here |
| `QA_MODEL_ID` | No | Unused, per `docs/deployment.md`'s existing note |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **Yes** | Still needed for Sheets sync — roadmap open decision #2 |
| `CONFIG_SHEET_ID` | No | Not secret, but environment-specific |
| `CONFIG_SYNC_CRON` | No | Confirm the `0 * * * *` override that fixed the 2026-08-12 beat-schedule/OOM collision (`render.yaml`'s comment) carries forward here too |
| `GEMINI_API_KEY` | **Yes** | |
| `GEMINI_RATE_LIMIT_PER_MINUTE` / `GEMINI_REQUEST_TIMEOUT_SECONDS` | No | Defaults fine |
| `SENTRY_DSN` | **Yes**, if set | Empty is valid (no error reporting) until Phase 11 |

Secrets referenced via App Runner's native Secrets Manager integration (`--secrets` in the service configuration, resolved at container start, never baked into the image or the CI workflow's logs).

### Checkpoint 2

- [ ] `docs/deployment-aws.md` lists every env var `app/config.py::Settings` declares, cross-checked line-by-line the same way `docs/deployment.md`'s table was — not written from memory
- [ ] Health check path and port match what `scripts/render_start.sh` actually binds — checkable by reading the script, no AWS account needed
- [ ] **User-only:** a real App Runner service actually starts, passes its health check, and serves `GET /api/v2/health` with a real `200` — needs the AWS account, ECR repository, and App Runner service to exist first

---

## Step 3 — Rollback runbook

### What to do

`docs/deployment-aws.md`'s rollback section: App Runner keeps a deployment history per service. Document the real commands:

```bash
aws apprunner list-operations --service-arn <service-arn>
aws apprunner start-deployment --service-arn <service-arn>   # redeploys the currently configured image/tag
```

Since deploys here are image-tag-based (Step 1 pushes a commit-SHA-tagged image), a rollback is: update the App Runner service's source image tag back to a known-good SHA, then `start-deployment`. Document this as the primary rollback path rather than relying only on App Runner's own deployment history UI, since the image tag is the actual source of truth here — same reasoning `docs/deployment.md` used for Fly's release-image rollback.

Note explicitly, same as `docs/deployment.md` already does for Fly: a rollback does **not** re-run `alembic upgrade head`. If the incident was caused by a migration, the fix is a forward migration, not a rollback — `docs/conventions.md`'s "migrations are forward-only in production" rule applies here exactly as it does on every other deploy path.

**Migration-on-deploy:** App Runner has no first-class release-command hook the way Fly's `[deploy] release_command` does. Run `alembic upgrade head` inside `scripts/render_start.sh` before the worker/uvicorn processes start, unchanged — the script already does exactly this today for the Render path, and it carries forward to App Runner with zero modification, which is another point in App Runner's favor per `0003`.

### Checkpoint 3

- [ ] `docs/deployment-aws.md`'s rollback section names real, correct `aws apprunner` commands
- [ ] Confirmed by reading `scripts/render_start.sh`: migrations still run before any process serves traffic, unmodified for this path
- [ ] **User-only:** an actual rollback against a real prior App Runner deployment — needs real deploy history to roll back from

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file.
2. Test each one that's provable without an AWS account for real: parse the workflow YAML, build the Docker image locally, cross-check the secrets table against `app/config.py::Settings`.
3. Return a structured report, explicitly separating what was verified from what remains user-only, the same way Phase 12's did:
   ✅ [Checkpoint] — Pass
   ⚠️ [Checkpoint] — Partial: [specific reason]
   ❌ [Checkpoint] — Fail: [specific reason]
   👤 [Checkpoint] — User-only, not this session's to prove
4. Fix all failures and partials that are within this session's control before reporting phase complete. Do not fabricate evidence for the 👤 items.
5. Update `phases/phase-roadmap.md` (mark this phase's row, note the Render free-tier path's new status as fallback-not-primary) and `CLAUDE.md` if the deployment target is worth a reader knowing without opening this file.
6. Only say "Phase 17 Complete" when every session-provable checkbox is green and docs are in sync. The 👤 items close when the user reports back that the real deployment is live — that's a separate, later confirmation, same as Phase 12's Fly path never fully closed and the Render path only closed once it was actually run.

## Final Phase 17 Checklist

- [ ] CI/CD workflow for AWS built and OIDC-authenticated, verified as valid config
- [ ] `docs/deployment-aws.md` complete with a cross-checked secrets table and a real rollback runbook
- [ ] Existing container and `scripts/render_start.sh` unchanged — App Runner runs what already works
- [ ] Self-audit passed with all green on everything session-provable; 👤 items explicitly listed, not glossed over
- [ ] `docs/`, `phases/phase-roadmap.md`, `CLAUDE.md` updated
- [ ] Manual verification done by architect once the user has completed the account-side setup
