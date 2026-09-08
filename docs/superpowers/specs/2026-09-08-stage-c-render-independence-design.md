# Stage C (reordered) — Render Independence via EC2 + Docker Compose

**Date:** 2026-09-08
**Status:** Design, approved in conversation; not yet implemented
**Supersedes for sequencing:** `2026-09-07-client-aws-migration-design.md`'s
Stage A → B → C ordering (see "Deviation from the staged plan" below).

## Goal

Stop both services depending on Render. Nothing else.

`jewelry-api` (V2) and `jewellery-gen-backend` (V1) currently run as free-tier
Render Web Services. This design moves both onto one EC2 instance running their
existing Docker images under Docker Compose, and replaces the one Render-managed
Redis with a container.

**It does not migrate the database, does not complete the S3 storage cutover,
and does not set up a domain or TLS.** Those are separate, independently
sequenced pieces of work — see Non-goals.

## Why this is being done before Stage A's live verification

The original design sequenced A (S3 storage) → B (RDS) → C (EC2 compute), each
verified live before the next, so only one variable moves at a time. That
ordering assumed live client traffic worth protecting.

There is none yet — the client is not using the system. The user chose to
reorder deliberately, accepting that Stage A's live verification (Task 9) is
still outstanding. This is a real deviation, recorded rather than glossed:

- **What the original ordering bought:** a small blast radius per stage.
- **What reordering costs:** when Task 9 finally runs, it runs against EC2
  rather than Render, so a storage failure and a compute-environment failure
  could present at the same time. Mitigated by the fact that this design
  changes *nothing* about storage configuration — each service keeps the exact
  storage env vars it has today, so a storage failure after cutover is
  attributable to the move only if those values were mistyped, which step 4 of
  the cutover checks directly.

## Current Render dependencies — the actual audit

| Dependency | Service | Render-coupled? | Action |
| :--- | :--- | :--- | :--- |
| Web Service (uvicorn + Celery worker + beat) | V2 | **Yes** | Move to EC2 |
| Web Service (uvicorn + in-process ARQ worker) | V1 | **Yes** | Move to EC2 |
| Key Value Redis `jewelry-api-redis` | V2 | **Yes** | Replace with container |
| Redis (Upstash) | V1 | No — already external | **Leave untouched** |
| Postgres (Supabase) | V2 | No — already external | Leave untouched |
| Object storage (Supabase Storage / S3) | both | No — already external | Leave untouched |
| `render.yaml`, `scripts/render_start.sh` | V2/V1 | Yes, but harmless | Keep in repo, unused |

Two findings from the audit, both load-bearing for this design:

1. **V1's Redis is not on Render.** It is Upstash, and it is V1's *primary job
   store* (`app/store/redis_store.py` — `job:{job_id}` hashes, 48h TTL, with
   `app/store/rehydrate.py` rebuilding from Google Sheets inside a 48h window).
   Moving it would destroy recent job records for no gain toward this goal, so
   this design does not touch it.
2. **V2's Render Redis has `persistenceMode: off` already.** Replacing it with
   an ephemeral container loses nothing that a Render Redis restart did not
   already lose, and `docs/schema.md`'s "What lives in Redis" section already
   guarantees every key there is reconstructible.

## Non-goals

Explicitly out of scope, to keep this one implementation plan:

- **Task 9 / S3 live verification.** Storage config is carried across
  byte-for-byte. Whatever works (or does not) on Render today works (or does
  not) identically on EC2.
- **Stage B / RDS.** The database stays on Supabase.
- **Domain, DNS, TLS.** No domain exists yet. Services are reached by
  `http://<elastic-ip>:<port>` until one does. Adding a reverse proxy later is
  additive and breaks nothing here.
- **App Runner.** `docs/deployment-aws-runbook.md` and
  `scripts/aws_provision.sh` (both currently uncommitted) describe the
  superseded Phase 17 App Runner path. This design does not use them. They are
  left in place, untouched, for the user to keep or discard.
- **CI/CD to EC2.** Deploys are `git pull && docker compose up -d --build`, run
  by hand. Automating it is worth doing later and is not needed to become
  Render-free.

## Architecture

One EC2 instance, two independent Compose stacks:

```
EC2 t3.small (Ubuntu 22.04, Elastic IP)
├── /opt/jewelry/jewelry-api/              (V2, git clone of main)
│   ├── docker-compose.yml                 (existing, unmodified)
│   ├── docker-compose.prod.yml            (NEW — overlay)
│   └── .env                               (populated from Render dashboard)
│       ├── api      → host :8000
│       ├── worker   (no host port)
│       ├── beat     (no host port)
│       └── redis    (internal only — replaces jewelry-api-redis)
│
└── /opt/jewelry/jewellery-gen-backend/    (V1, git clone of master)
    ├── docker-compose.yml                 (existing, unmodified)
    ├── docker-compose.prod.yml            (NEW — overlay)
    └── .env                               (populated from Render dashboard)
        ├── api      → host :8001
        └── worker   (no host port)
        (no redis service — V1 keeps Upstash)
```

Each stack is brought up with both files:
`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`

The base compose files are never edited. The overlay carries every difference
between "developer laptop" and "production host", so the two can't silently
drift and a developer running plain `docker compose up` still gets the dev
behaviour they expect.

### Why one instance, not two

Two instances would give process isolation between V1 and V2. At current
traffic (zero) and current footprint (V2's whole stack idled under 512MB on
Render; V1 is lighter), that isolation buys nothing and doubles both cost and
the number of places to run a deploy. One instance, revisited if real load
justifies splitting.

### Cost

| Item | Monthly (approx, on-demand) |
| :--- | :--- |
| t3.small | ~$15-17 depending on region |
| Elastic IP (attached to a running instance) | $0 |
| EBS 20GB gp3 | ~$1.60 |
| **Total** | **~$17-19** |

For contrast, the superseded App Runner shape in
`docs/deployment-aws-runbook.md` required a NAT Gateway (~$32-35/mo) and an
ElastiCache node (~$12-13/mo) *before* App Runner's own compute charge, because
a VPC-attached App Runner service routes all egress through NAT and ElastiCache
has no public endpoint. Running Redis as a container next to the app on a
public-subnet EC2 instance removes both line items outright.

## Repository changes

This is not purely an infra exercise — three real gaps make the existing
compose files unsafe to run as production, and each needs a committed fix.

### V2 (`jewelry-api`) — new `docker-compose.prod.yml`

| Problem | Fix in overlay |
| :--- | :--- |
| `worker` runs `-c ${IO_QUEUE_CONCURRENCY:-20}` under the prefork pool. Render measured ~154MB per forked child once `google-genai` is imported; 20 children is ~3GB and would OOM a 2GB instance on the first burst. | Pin `IO_QUEUE_CONCURRENCY=2` in `.env`, and set it explicitly in the overlay so the `:-20` default can never apply. |
| No `restart:` policy on any service — a crashed container stays down, and nothing survives instance reboot. | `restart: unless-stopped` on all four services. |
| **No `alembic upgrade head` anywhere.** `scripts/render_start.sh` was also V2's migrate-on-deploy mechanism; Compose has no equivalent, so moving to EC2 silently drops migrations from the deploy path. | A `migrate` one-shot service (`command: alembic upgrade head`, `restart: "no"`), which `api`/`worker`/`beat` depend on with `condition: service_completed_successfully`. Migrations then run exactly once per `up`, before anything serves traffic — matching Render's behaviour rather than relying on the operator remembering a manual step. **Implementation note:** the base file uses `depends_on`'s short list syntax (`[redis]`); an override file *replaces* a sequence rather than merging it, so the overlay must restate redis in long form (`redis: {condition: service_started}`) alongside `migrate`, or the redis dependency is silently dropped. **Correction, 2026-09-08:** this claim is wrong the same way the plan file's own earlier version was — Compose actually merges `depends_on` by key union, so restating `redis` alongside `migrate` in the overlay is harmless but not strictly required; new keys merge in without needing to restate existing ones. Only *clearing* an existing key to nothing needs the explicit `!reset` tag, which is a different situation from this migrate/redis case. |
| `redis` publishes `6379:6379` to the host. | Drop the host port binding — the service is reachable on the Compose network by name; publishing it puts an unauthenticated Redis on a public IP. |

### V1 (`jewellery-gen-backend`) — new `docker-compose.prod.yml`

| Problem | Fix in overlay |
| :--- | :--- |
| `api` runs `uvicorn --reload` and bind-mounts `./app` and `./ui` from the host. This is a development configuration; in production it serves from mutable host state and runs the reloader. | Override `command` to drop `--reload`; override `volumes: []` so the image's own code is used. |
| Host port is `8000:8000`, which collides with V2 on the same instance. | Publish `8001:8000`. |
| `redis` service exists in the base file and would start a second, unused Redis. | Not started — the prod overlay runs only `api` and `worker` (V1 keeps Upstash). Enforced by naming services explicitly on the `up` command rather than by deleting the base service. |
| `WORKER_IN_PROCESS=true` on Render (worker runs inside the API process). With a separate `worker` container that would run the worker loop twice. | `WORKER_IN_PROCESS=false` in `.env` — stated explicitly in the env checklist because it is the one value that must *differ* from Render's. |
| No `restart:` policy. | `restart: unless-stopped`. |

**Correction, 2026-09-08 (found while writing the implementation plan):**
the table above understated what the V1 overlay needs. Running `docker
compose config` against the unmodified base file showed two things not
caught when this design was written:

1. `api`/`worker` both hardcode `environment: REDIS_URL:
   redis://redis:6379/0`, which wins over `env_file:` for the same key
   regardless of what `.env` sets. Fixed by setting
   `environment: REDIS_URL: ${REDIS_URL}` in the overlay, which
   Compose interpolates from the same `.env` file at parse time.
2. Both declare `depends_on: {redis: {condition: service_healthy}}`.
   Compose starts a service's dependencies whether or not they're named
   on the `up` command line, so naming only `api worker` on `up` was not
   sufficient on its own to keep the local `redis` service from starting.
   Fixed with `depends_on: !reset {}` on both in the overlay, in addition to
   (not instead of) always naming services explicitly in the runbook.

Both fixes are implemented in `docker-compose.prod.yml`
(`docs/superpowers/plans/2026-09-08-ec2-render-independence.md` Task 3).

### Both repos — documentation

`docs/deployment-ec2.md` in each repo: what runs where, how to deploy an
update, how to roll back to Render. Neither repo's existing deployment docs are
deleted — Render and Fly paths stay documented as fallbacks, consistent with
how this project has always kept superseded paths side by side.

## Environment variables

Every value is copied from the service's current Render dashboard (Environment
tab) into the EC2 `.env`, unchanged, **except** the ones below. Render masks
secret values in edit mode (`data-state="hidden"`) — that is masking, not loss;
do not re-enter a field that looks blank.

### V2 — values that change

| Variable | Render (today) | EC2 |
| :--- | :--- | :--- |
| `REDIS_URL` | `jewelry-api-redis` internal URL | `redis://redis:6379/0` |
| `CELERY_BROKER_URL` | same instance, db 1 | `redis://redis:6379/1` |
| `CELERY_RESULT_BACKEND` | same instance, db 2 | `redis://redis:6379/2` |
| `IO_QUEUE_CONCURRENCY` | unset (inert under `--pool=solo`) | irrelevant on this path — `docker-compose.prod.yml` hardcodes `-c 2` directly in the worker command, this env var is not read there |

Everything else — `DATABASE_URL`, `GEMINI_API_KEY`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `CONFIG_SHEET_ID`, `S3_REGION`, `BUCKET_INPUTS`,
`BUCKET_OUTPUTS`, `QA_*`, `MASK_*`, `WORKING_MAX_EDGE`, `APP_ENV`, `MOCK_MODE`,
`CONFIG_SYNC_CRON`, `SENTRY_DSN` — carries across verbatim.

### V1 — values that change

| Variable | Render (today) | EC2 |
| :--- | :--- | :--- |
| `WORKER_IN_PROCESS` | `true` | `false` |

`REDIS_URL` keeps its existing Upstash value. Everything else — `API_KEYS`,
`ADMIN_API_KEY`, `GOOGLE_SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`,
`SUPABASE_*`, `STORAGE_BACKEND`, `GEMINI_API_KEY`, `PROVIDER`,
`CORS_ALLOWED_ORIGINS`, limits — carries across verbatim.

**Confirm before cutover, do not assume:** open each service's Render
Environment tab and check what `REDIS_URL` actually points at. This design
assumes V2's is the Render Key Value instance and V1's is Upstash. If V1's also
points at Render, V1 needs a Redis container too and its 48h job state will not
survive the move (recoverable via `app/store/rehydrate.py` from Sheets).

## Infrastructure

**Instance:** t3.small, Ubuntu 22.04 LTS, 20GB gp3, Elastic IP attached.
2GB RAM against a measured footprint of ~400MB (V2 idle) + ~150MB (V1 idle) +
2 × ~154MB (V2 prefork children at `IO_QUEUE_CONCURRENCY=2`) + ~30MB (Redis)
≈ 1.1GB peak, leaving real headroom rather than a guess.

**Security group:**

| Port | Source | Purpose |
| :--- | :--- | :--- |
| 22 | operator's IP only | SSH |
| 8000 | `0.0.0.0/0` | V2 API |
| 8001 | `0.0.0.0/0` | V1 API |

No inbound 6379. V2's Redis is container-internal; V1's is Upstash-hosted and
reached outbound.

Both APIs remain protected by their own `X-API-Key` auth, so opening 8000/8001
exposes no unauthenticated surface — only the unauthenticated `/health`
endpoints, which is the same posture Render already has.

**Reboot survival:** Docker's daemon is enabled at boot by the standard install,
and `restart: unless-stopped` brings every container back with it. No systemd
unit is written — adding one would duplicate what the restart policy already
guarantees.

## Cutover and rollback

Render keeps running, untouched, throughout. Nothing is deleted.

1. Provision the instance, Elastic IP, and security group (AWS console).
2. Bootstrap: install Docker + Compose plugin; clone both repos to
   `/opt/jewelry/`.
3. Populate both `.env` files from the Render dashboards.
4. Bring both stacks up. Verify **from the instance** (`curl localhost:8000/api/v2/health`,
   `curl localhost:8001/health`), then **externally** by Elastic IP.
5. Verify beyond health: V2's `/ui` page loads and `GET /api/v2/config` returns
   the real active config version — this exercises Postgres and Redis through
   the app, which the health endpoint's own checks also do, but via the real
   request path.
6. Repoint whatever currently calls the Render URLs (the `/ui` client, and the
   Flutter ERP if it is pointed anywhere yet) at the Elastic IP.
7. **Suspend, do not delete,** both Render services and `jewelry-api-redis`,
   after 48 hours of the EC2 instance behaving. Deletion is a separate,
   later, deliberate step.

**Rollback** at any point before step 7: resume the Render services (or simply
stop repointing at EC2). Render is still live and still correct throughout —
this design never modifies a Render service, only stops depending on it.

### What health checks will and will not tell you

`GET /api/v2/health` checks Postgres and Redis for real. **It reports
`storage: "ok"` unconditionally — a hardcoded literal in
`app/api/v2/health.py`, not a probe.** So a green health response says nothing
whatsoever about object storage.

This matters twice: it is why step 5 above adds a real request-path check, and
it means the outstanding Task 9's own instruction to "confirm health reports
`storage: ok`" was never a meaningful verification. Recorded here; fixing the
health endpoint is out of scope for this design.

## AWS region — decided

**`ap-south-1` (Mumbai).** Confirmed 2026-09-08. Matches both repos'
`.env.example` defaults (`S3_REGION`/`AWS_REGION=ap-south-1`) and the
client/ERP's India location. The superseded App Runner runbook's `us-east-1`
is not reused.

## Success criteria

1. Both APIs answer over the Elastic IP, with real config data from Supabase.
2. V2's Celery worker consumes from the container Redis — verifiable by
   submitting a job and watching a sub-job leave `PENDING`.
3. Rebooting the instance brings every container back with no manual step.
4. Both Render services can be suspended with no observable change.
5. No repository's storage, database, or provider configuration was altered.
