# Phase 12 — CI/CD & Deployment

## Reality check before writing this

This phase is a different kind from every phase before it. Phases 0–10
could all be built *and verified live* against something already
real — a real Supabase project, real local Redis, real testcontainers
Postgres. This phase cannot: there is no Fly.io account, no Upstash
account, no `FLY_API_TOKEN`, and no live deployment anywhere. Creating
those accounts and entering payment/credential details is not something
this session does on the user's behalf — that decision and that setup are
the user's to make. **What this phase can honestly produce is real,
deployable configuration and a real CI→CD pipeline definition — not a
verified live deployment.** Every checkpoint below says explicitly whether
it's something this session can prove, or something only the user can prove
by actually running it.

**What's already real** (Phase 0, re-verified not re-built):
`.github/workflows/ci.yml` already runs `ruff check`, `ruff format --check`,
`mypy --strict`, `alembic upgrade head` against a real ephemeral Postgres
service container, `pytest --cov`, and `docker build` — a genuine CI
pipeline, not a skeleton missing pieces. This phase adds the **CD** half
that doesn't exist yet: nothing currently deploys anywhere.

**Target decided with the user, not guessed**: **Fly.io** for the API +
worker + beat processes, **Upstash** for Redis (both have real ongoing free
tiers at this project's likely volume — Render's free tier doesn't cover
background workers, Railway's free tier is a one-time credit, not ongoing).
Supabase Postgres is unchanged — already in use since Phase 0.

**GPU host provisioning — the roadmap line's other half — is moot**, not
deferred: `docs/decisions/0001-drop-local-matting.md` already removed every
VRAM-bound workload from this codebase. Roadmap open decision #3 asked
"RunPod / Lambda / bare metal / GCP" for a GPU host that no longer needs to
exist. Marked resolved N/A below, same as open decision #1 was for the
matting model itself.

**A migration-on-deploy mechanism already has an obvious real answer**:
Fly's `[deploy] release_command` runs a command against the new release
*before* traffic shifts to it and fails the deploy if the command fails —
`alembic upgrade head` is exactly that command, and this is a genuine Fly
feature, not a workaround.

---

## Step 1 — Fly app configuration (staging + production)

### What to do

`fly.toml` (production, app name `jewelry-api`) and `fly.staging.toml`
(staging, app name `jewelry-api-staging`) — both built from the existing
`Dockerfile` (Phase 0, unchanged), both defining three Fly **process
groups** in one app rather than three separate apps (Fly machines, not
Nomad — process groups are the current mechanism):

- `app` — `uvicorn app.main:app --host 0.0.0.0 --port 8000`
- `worker` — `celery -A app.workers.celery_app worker -Q io -c
  ${IO_QUEUE_CONCURRENCY:-20}`
- `beat` — `celery -A app.workers.celery_app beat`

HTTP service (the `app` group only) health-checked against the real
`GET /api/v2/health` (Phase 0) — it already checks both Postgres and Redis,
so Fly's own health check reuses application logic instead of duplicating
it. `[deploy] release_command = "alembic upgrade head"`.

`.env.example`: add a comment block on the `REDIS_URL`/`CELERY_BROKER_URL`/
`CELERY_RESULT_BACKEND` vars documenting the Upstash `rediss://` (TLS) URL
shape — no code change needed, `app/config.py` already reads these as plain
strings and `redis.asyncio.from_url` already handles the `rediss://` scheme
(confirmed by reading `redis-py`'s own URL parsing, not tested live here —
no Upstash instance exists to test against).

### Checkpoint 1 — what this session can prove vs. what only the user can

- [x] `fly.toml` / `fly.staging.toml` are valid TOML (parsed in Step 4's
      self-audit) — mechanically checkable without a Fly account
- [ ] **User-only:** `flyctl deploy` actually succeeds against a real Fly
      app — needs a Fly account, `flyctl auth login`, and `fly apps create`
      run by the user; not something this session can execute or fake
      evidence for

---

## Step 2 — CD workflow

### What to do

`.github/workflows/deploy.yml`: two jobs, both `needs: ci` referencing the
existing `ci.yml` workflow (or duplicated steps if GitHub Actions'
`workflow_run` triggering proves simpler than `needs` across workflow
files — decided in implementation, documented there) so a red CI run can
never deploy:

- **`deploy-staging`** — triggers on every push to `main` after CI passes.
  `flyctl deploy --config fly.staging.toml`.
- **`deploy-production`** — triggers on a pushed tag matching `v*`.
  `flyctl deploy --config fly.toml`. Requires a human to cut a tag —
  deliberately not automatic on every `main` push, so production can't
  ship a change nobody decided to ship yet.

Both authenticate via a `FLY_API_TOKEN` GitHub Actions secret — **the user
must create this** (`fly tokens create deploy` after creating the Fly
account) and add it to the repo's secrets. This session cannot create a
GitHub Actions secret on the user's behalf.

### Checkpoint 2

- [x] `deploy.yml` is valid workflow YAML (`actionlint` or GitHub's own
      schema, checked in Step 4) — mechanically checkable
- [ ] **User-only:** a real push to `main` actually triggers a real staging
      deploy — needs `FLY_API_TOKEN` set in the repo, which needs the Fly
      account to exist first

---

## Step 3 — Rollback runbook

### What to do

Not code — a documented procedure, because Fly's own release history *is*
the rollback mechanism; building a custom one would duplicate a feature
that already exists. `docs/deployment.md` (new): `fly releases` lists
every deploy for an app; `fly deploy --image <previous-image-ref>`
re-deploys a prior image without rebuilding. Documents the exact commands
for both `jewelry-api` and `jewelry-api-staging`, plus the Fly secrets
checklist every fresh app needs before its first real deploy
(`DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `REDIS_URL`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `GEMINI_API_KEY`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `SENTRY_DSN` — every secret
`app/config.py` reads, set via `fly secrets set`, never committed).

### Checkpoint 3

- [x] `docs/deployment.md` lists every env var `app/config.py::Settings`
      declares — mechanically cross-checked against the real class in
      Step 4, not just written from memory
- [ ] **User-only:** an actual rollback against a real prior release —
      needs a real deploy history to roll back from

---

## Step 4 — Self-audit

Different shape than every prior phase's self-audit, for the reason stated
above. What this session validates for real: `fly.toml`/`fly.staging.toml`
parse as valid TOML; `deploy.yml` parses as valid YAML and its `needs`/
`on` triggers reference real, existing job/workflow names; every env var in
`docs/deployment.md`'s secrets checklist is cross-checked line-by-line
against `app/config.py::Settings` so the list can't drift from what the app
actually reads. Sync `phases/phase-roadmap.md` (mark open decision #3
resolved N/A, add the new deployment-target decision as a resolved entry
rather than a new open one, since the user decided it directly), `CLAUDE.md`.
**What this phase cannot self-audit into "done," stated plainly rather than
glossed over:** an actual successful deploy, a real health check passing
against a live Fly app, a real rollback. Those require the user to create
the Fly.io and Upstash accounts, run `flyctl auth login`, create the two
apps, set every secret above, and push — none of which this session can do
without account credentials it doesn't have and shouldn't be given.

---

## Note for Phase 13

Phase 13 (Load, Soak & Capacity Tuning) is listed sequential after this
one — but it needs a real running deployment to load-test against, which
this phase's own limitations mean doesn't exist yet either. Phase 13 can't
start for real until the user has actually completed the manual steps this
phase's checkpoints leave open.

---

## Addendum — a wrong assumption found and corrected, not silently fixed

Everything above was written and built assuming Fly.io had no cost of
entry. That was wrong: Fly.io has required a credit card on every account
since a 2024 anti-abuse policy change, even to stay within the free
allowance — found only when the user actually tried to sign up, not by
anything checkable from this session. The user chose to switch the free
path to **Render + Upstash** instead — the same combination V1
(`jewellery-gen-backend`) already runs live in production, discovered
during this same conversation while investigating an unrelated question
about where V1's secrets lived.

Render's free tier has no free Background Worker (V2's Celery `worker`/
`beat` need one) and no free managed Redis — solved with
`scripts/render_start.sh` (runs `alembic upgrade head`, then backgrounds
`celery beat` and `celery worker -Q io`, then runs `uvicorn` in the
foreground — one process tree, one billed service) and Upstash's free
Redis, and `render.yaml` at the repo root. **This path was verified live,
locally** — not just config-parsed the way Step 4's self-audit could manage
for Fly: built the real Docker image, ran it with real Supabase
`DATABASE_URL` and a local Redis reachable via `host.docker.internal`,
confirmed via `docker top` that all three processes (`uvicorn`, `celery
beat`, `celery worker` with its prefork pool) were actually running inside
one container, and confirmed `GET /api/v2/health` returned `{"status":
"ok", "dependencies": {"db": "ok", "redis": "ok", "storage": "ok", ...}}`
— genuine end-to-end proof this shape works, closer to what every prior
phase's own tests could do than Fly's path ever got to in this session.

**A real, minor gap found and left open, not fixed:** the container runs
Celery as root (`python:3.12-slim`'s default user), which Celery itself
warns about at startup (`SecurityWarning: You're running the worker with
superuser privileges`). Not a blocker for a free-tier demo deploy, but
worth a non-root user in the Dockerfile before this goes anywhere near
real client traffic — flagged here rather than silently left for someone
to discover from a warning log later.

Both paths are kept, not one replacing the other — `docs/deployment.md`
(Fly, card required) and `docs/deployment-free-tier.md` (Render, cardless)
— mirroring the exact split V1's own two deployment docs already use.
`fly.toml`/`fly.staging.toml` and the Fly-specific half of
`.github/workflows/deploy.yml` are untouched and still valid if the user
ever decides the card is worth it for Fly's operational advantages (true
process isolation, no cold-start-while-sleeping gap).
