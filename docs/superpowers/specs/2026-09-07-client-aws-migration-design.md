# Migration to the client's AWS account — design

**Date:** 2026-09-07
**Scope:** `jewelry-api` (V2) and `jewellery-gen-backend` (V1)
**Goal:** zero dependency on Render and zero dependency on Supabase. Both
services run on EC2 in the **client's own AWS account**, backed by RDS and S3,
exposed over HTTPS on a client-supplied domain for their ERP team to integrate
against.

Supersedes the App Runner target in `docs/deployment-aws.md` and
`docs/decisions/0003-deploy-to-aws.md`. Those documents are not deleted — the
same precedent Phase 12 set when Render was added alongside Fly — but App
Runner is no longer the destination. See "Why EC2, not App Runner" below.

---

## 1. Where we are today

Verified against the live Render account (workspace `tea-d96id86q1p3s73c6rh70`)
and both repositories on 2026-09-07.

| | V1 `jewellery-gen-backend` | V2 `jewelry-api` |
|---|---|---|
| Render service | `srv-d9m8m1jm8hqs73a68bbg`, free, Oregon | `srv-d9s46ifavr4c73ae6oc0`, free, Oregon |
| URL | `jewellery-gen-backend.onrender.com` | `jewelry-api-qc8b.onrender.com` |
| Branch | `master` | `main` |
| Process model | API + ARQ worker in one process (`WORKER_IN_PROCESS=true`) | API + Celery worker + beat in one container (`scripts/render_start.sh`) |
| Queue | Upstash Redis (free) | Render Key Value `red-da2189vqj5pc73dcbugg` (free) |
| Database | none | Supabase Postgres (`ap-northeast-1` pooler) |
| Object storage | Supabase Storage, via `app/storage/` adapter | Supabase Storage, via `app/services/storage_service.py` |
| Config source | Google Sheets | Google Sheets |
| Generation | Gemini | Gemini |

There are **no Render Postgres instances** in the account. Render provides
exactly six things to these services: Docker build, auto-deploy on push, a TLS
URL, the V2 Redis instance, restart-on-crash supervision, and env-var storage.

## 2. Target architecture

One EC2 instance in the client's AWS account, running every process as a
container on a single Docker network. Region is the client's decision; the
design is region-agnostic, but **compute, RDS and S3 must all land in the same
region** — the whole reason to leave Supabase is to stop paying a cross-region
round trip on every database call.

```
                    Internet
                       |
              [ Elastic IP :443 ]
                       |
   +-------------------------------------------+
   |  EC2 t4g.medium (2 vCPU / 4 GB), AL2023    |
   |                                            |
   |   caddy ── TLS termination, subdomain      |
   |     |      routing, the ONLY published     |
   |     |      ports (80/443)                  |
   |     +---> v2-api  (uvicorn)                |
   |     +---> v1-api  (uvicorn + ARQ in-proc)  |
   |                                            |
   |   v2-migrate (one-shot: alembic upgrade)   |
   |   v2-worker  (celery -Q io --pool=solo)    |
   |   v2-beat    (celery beat)                 |
   |   redis      (network-internal only)       |
   +-------------------------------------------+
             |                      |
     [ RDS PostgreSQL ]        [ S3 buckets ]
      private subnet             VPC endpoint
```

### Instance sizing

`t4g.medium` — 2 vCPU, 4 GB. The 4 GB is not padding. RECOLOR and MIX composite
at the upload's full resolution to guarantee byte-identical output outside the
mask/seam band; a 3072×4096 client upload is a **37.7 MB decoded RGB buffer**,
and the final compositing steps hold five or six of those at once. That is what
OOM-killed the 512 MB Render tier repeatedly
(`docs/incident-2026-08-25-recolor-mix-oom.md`). Reason about image memory as
`width × height × channels × buffers alive`, never as file size on disk.

**ARM caveat.** Graviton requires `linux/arm64` images. Both services are pure
Python on `python:3.12-slim`; Pillow, boto3 and google-genai all publish arm64
wheels, so this should be a `docker buildx --platform linux/arm64` flag and
nothing more. Task 1 of the implementation plan verifies this with a real build
before any infrastructure is provisioned. If anything fails to resolve, fall
back to `t3.medium` (x86, ~$6/mo more) — no other part of this design changes.

### Process model: V2 splits into separate containers

`scripts/render_start.sh` runs API, worker and embedded beat in one process
tree with `--pool=solo`, and ends with `wait -n … ; cleanup; exit 1` so that
**any one process dying tears down all three**. That was correct for Render:
the free tier bills per service, and a half-dead stack serving traffic with no
worker is worse than a restart.

On EC2 that constraint disappears. Each process becomes its own container with
`restart: unless-stopped`, so a worker crash no longer 502s the API. Concretely
this retires three Render-shaped workarounds:

- the solo pool (chosen only to fit 512 MB) can return to prefork if load ever
  justifies it, though it stays solo initially — see "Deliberate non-goals";
- embedded beat (`-B`) becomes a real separate container, removing the
  documented multi-scheduler caveat entirely;
- `alembic upgrade head` moves out of the start script into a one-shot
  `v2-migrate` service that api/worker gate on via
  `depends_on: { condition: service_completed_successfully }`.

`scripts/render_start.sh` **stays in the repository** — `fly.toml` still
references the image's default CMD — but nothing in this path invokes it.

### Redis

A `redis:7-alpine` container on the box, attached to the internal Docker
network and **never published to the host**. No ElastiCache: Redis is not the
system of record in either service (`CLAUDE.md`'s architecture note), so
managed persistence and backups buy nothing, and ElastiCache would force a VPC
connector and a NAT Gateway that this design otherwise avoids.

Both services already take a DSN from the environment — V2 through Celery and
`app/core/redis_client.py`, V1 through `RedisSettings.from_dsn` — so this is a
**configuration change with no application code**:

| Setting | Value |
|---|---|
| `CELERY_BROKER_URL` (V2) | `redis://redis:6379/0` |
| `CELERY_RESULT_BACKEND` (V2) | `redis://redis:6379/1` |
| `REDIS_URL` (V2) | `redis://redis:6379/2` |
| `REDIS_URL` (V1) | `redis://redis:6379/3` |

Two traps must be carried forward deliberately:

1. **Drop the `?ssl_cert_reqs=required` suffix** currently appended to both V2
   Celery URLs in the Render dashboard. It exists because Upstash was TLS-only;
   this Redis is plain `redis://` and the parameter is meaningless here.
   (`app/workers/celery_app.py` sets `CERT_REQUIRED` itself for any `rediss://`
   URL, so nothing is lost.)
2. **Set `maxmemory-policy noeviction`** in `redis.conf`, matching the Render
   Key Value instance's configuration. Redis's default `allkeys-lru` would
   silently evict Celery task state under pressure.

No data migration: persistence is off on the Render instance and neither
service treats Redis as durable.

### Database: Supabase Postgres → RDS

V2 only. V1 has no database.

RDS PostgreSQL, `db.t4g.micro` to start, single-AZ, in a **private subnet** —
no public endpoint, reachable only from the EC2 security group on 5432. Version
must match the current Supabase major version; confirm before dumping.

V2 talks to it through SQLAlchemy with Alembic migrations, so this is a
`DATABASE_URL` change plus a data move. **No ORM or model changes.**

The migration is `pg_dump` → `pg_restore`, not `alembic upgrade head` against
an empty database, because production data must survive — in particular
`config_versions`, whose active version carries the `model_version`
(`gemini-3.1-flash-image`) that Alembic data migration `0005` set and that the
Google Sheet can never restore, since the real Sheet has no Global tab.

Percent-encode any reserved character in the new RDS password. The current
Supabase password contains a raw `@`, which SQLAlchemy splits on, producing a
`socket.gaierror` that reads like a DNS outage and is purely a URL-parsing bug.
That is a documented trap in `docs/deployment.md`; do not reintroduce it.

**This also closes an outstanding security item.** The Supabase password was
printed into a session transcript in August and has never been rotated. Moving
to RDS retires that credential entirely rather than rotating it.

### Object storage: Supabase Storage → S3

Buckets stay private. There is no public read path in either service today and
this does not introduce one; every client-facing URL remains a time-limited
presigned URL, with TTL from the existing `SIGNED_URL_TTL_SECONDS`.

Access is via an **EC2 instance profile**, not access keys — boto3 picks up
instance credentials with no configuration. A **gateway VPC endpoint for S3**
keeps that traffic off the public internet and off the NAT path.

The two services need different work here, and the difference matters for
sequencing:

**V1 is a clean extension.** `app/storage/` already defines a `StorageAdapter`
Protocol (`put` / `get` / `exists`) with a factory dispatching on
`STORAGE_BACKEND`, and already has three implementations (`local`, `supabase`,
`drive`). Adding S3 is one new `app/storage/s3.py`, one factory branch, and the
config fields — with **no change to any code outside `app/storage/`**, which is
exactly what that Protocol's docstring promises. `STORAGE_BACKEND=s3` then
selects it.

**V2 is a contained rewrite.** All Supabase coupling lives in
`app/services/storage_service.py` (plus two config fields); thirteen call sites
across the API and services layer use only its eleven module-level functions.
The internals get reimplemented on boto3 while **every signature stays
identical**, so no caller changes:

| Function | S3 implementation |
|---|---|
| `generate_upload_url` | presigned PUT |
| `generate_signed_url` | presigned GET |
| `download_to_temp` / `download_bytes` | `get_object` |
| `upload_from_temp` / `upload_bytes` | `put_object` |
| `exists` | `head_object` |
| `delete` | `delete_object` |
| `build_storage_path` | unchanged — the `{job_id}/{angle}/{kind}_{uuid}.{ext}` convention is already an S3-shaped key |

The `_with_retries` wrapper stays, with its catch narrowed to botocore's
transport-level exceptions. Its existing discipline must be preserved exactly:
retry only failures where **no HTTP response was received**, never a real error
response (`NoSuchKey`, `AccessDenied`), because retrying a deterministic
failure silently swallows it. boto3's own adaptive retry mode covers part of
this; the wrapper stays as the explicit, tested boundary.

### Ingress

Caddy in a container, terminating TLS with automatic Let's Encrypt
certificates, routing by subdomain to `v2-api` and `v1-api`. Elastic IP, A
records supplied by the client. No ALB: at one instance it would add ~$18/mo
and a second health-check surface for no availability gain.

Security group: **443 and 80 inbound from `0.0.0.0/0`, nothing else.** Port 80
exists only for the ACME HTTP challenge and redirects to 443. **No inbound
SSH** — administrative access is SSM Session Manager, which also removes any
key-pair to distribute or rotate.

### Networking

A VPC with public and private subnets. EC2 sits in the public subnet with the
Elastic IP, so its egress to Gemini and the Google Sheets API needs no NAT
Gateway (~$32/mo saved — this was the single largest line item in the App
Runner plan, whose VPC connector never assigns a public IP). RDS sits in the
private subnets. S3 goes through a gateway endpoint.

## 3. Deploy pipeline

`ci.yml` is unchanged. `deploy-aws.yml`'s OIDC-and-ECR half survives, repointed
at the client's account; the `aws apprunner start-deployment` call is replaced:

```
push to main   → build arm64 → ECR :staging
push tag v*    → build arm64 → ECR :vX.Y.Z → ssm send-command → box pulls & recreates
```

Authentication stays **GitHub OIDC with `role-to-assume`** — no long-lived
access keys in repository secrets. This needs an OIDC identity provider and an
IAM role in the client's account whose trust policy is scoped to the specific
repository; that is the client's action, listed in §7.

The SSM command runs a `deploy.sh` on the instance that does
`docker compose pull && docker compose up -d`, and **fails the workflow if the
health check does not come back green**, so a bad deploy is visible in Actions
rather than only in production. The instance profile carries
`AmazonSSMManagedInstanceCore`, ECR pull, S3 access for the buckets, and SSM
Parameter Store read.

V1 currently has no deploy workflow at all — it inherits a parallel one.

## 4. Configuration and secrets

SSM Parameter Store `SecureString` under `/jewelry/v2/*` and `/jewelry/v1/*`,
read at container start into the compose environment. Nothing secret is written
into `docker-compose.yml`, the AMI, or the repository.

Retired by this migration: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_STORAGE_BUCKET`. Added: `S3_BUCKET_*`,
`AWS_REGION`, and a `DATABASE_URL` pointing at RDS. `GEMINI_API_KEY`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `CONFIG_SHEET_ID`, `API_KEYS`, `ADMIN_API_KEY`
and `SENTRY_DSN` carry over unchanged.

`~/jewelry-api/.env` remains the authoritative known-good reference to diff
against, as it has been throughout. Do not transcribe secrets from screenshots
— that has already cost one deploy cycle on this project.

## 5. Sequencing

The three changes are independently shippable, and the order is chosen so that
**each one is verified while the previous platform is still running**:

| Stage | Content | Verified where |
|---|---|---|
| **A. S3** | V1 `S3Storage` adapter; V2 `storage_service` on boto3; tests against moto | On **Render**, pointed at the client's S3 |
| **B. RDS** | Provision RDS, `pg_dump`/`pg_restore`, repoint `DATABASE_URL` | On **Render**, pointed at RDS |
| **C. EC2** | VPC, instance, compose stack, Caddy, pipeline, DNS cutover | On EC2 |

This is the central risk decision in this design. Doing storage and database
migrations *while still on Render* means that when the compute finally moves,
the only variable that has changed is where the container runs — every other
dependency is already proven in production. The alternative (a single
big-bang cutover) makes any failure ambiguous between four simultaneous
changes, on a client-facing system.

Stage A and Stage B can each be rolled back by reverting one environment
variable, for as long as the Supabase project stays alive.

## 6. Verification

A green health check is **not** the acceptance gate. `/api/v2/health` reports
db/redis/storage reachability, which the 512 MB Render instance also reported
happily between OOM kills.

**Stage A:** a full round trip per service — upload, generate, presigned
download — with the returned image opening as a real image. Not just a
200-with-bytes: the August base64 bug produced files of exactly the right
length, entirely wrong content, a COMPLETED status, and a clean download that
simply would not open. Byte-compare a downloaded output against the uploaded
source for a passthrough path.

**Stage B:** `config_versions` active version is 3 with `model_version`
`gemini-3.1-flash-image`; row counts match the dump; a fresh job writes and
reads back.

**Stage C, the real gate:** one **RECOLOR** and one **MIX** job at full
3072×4096 resolution completing, with `docker stats` peak RSS captured for the
worker container. This is the exact workload that killed the previous platform,
and the memory headroom claim is unproven until it is measured. Plus one V1
generation end to end, and a deliberate `docker kill` of `v2-worker` confirming
the API stays up — the behaviour change that separate containers are for.

Note that job submission requires an `X-API-Key`, so the end-to-end runs are
the user's step, through the `/ui` page.

## 7. Cutover and rollback

1. Stages A and B complete and verified on Render.
2. Stage C deployed; all Stage C verifications pass against the Elastic IP
   directly, before any DNS exists.
3. Client points the agreed subdomains at the Elastic IP; Caddy issues
   certificates.
4. **The client's ERP team integrates against the new domain.** They own that
   integration; our deliverable is the documented API at a stable HTTPS URL.
   Because the ERP integration is being built fresh against the new domain, no
   existing consumer needs repointing. This assumes the current
   `.onrender.com` URLs are held only by our own `/ui` page, which is served
   from the same container and moves with it — confirm before teardown that
   nothing else (a client test harness, an automation) is calling them.
5. Render services left running, untouched, for 48 hours as rollback.
6. Delete the Render web services and the `jewelry-api-redis` Key Value
   instance. Delete the Supabase project only after the client confirms the
   dump is retained.

Rollback within the 48-hour window is a DNS change back to Render, which still
holds the pre-Stage-A configuration only if Supabase is still live — so
**Supabase must not be torn down until step 6.**

## 8. Cost

| Item | Monthly |
|---|---|
| EC2 `t4g.medium` on-demand | ~$24 |
| 30 GB gp3 | ~$2.40 |
| RDS `db.t4g.micro` single-AZ + 20 GB | ~$15 |
| S3 (low volume) + ECR | ~$2 |
| Elastic IP (attached) | $0 |
| **Total** | **~$44** |

Reserved instances or Savings Plans would cut the EC2 and RDS lines
substantially; that is the client's commercial decision once usage is real.

For contrast, the superseded App Runner design costs more for less control:
2 GB App Runner plus ElastiCache plus a mandatory NAT Gateway is ~$70+/mo,
where the NAT Gateway alone is ~$32 and exists purely because App Runner's VPC
connector never assigns a public IP.

## 9. Why EC2, not App Runner

`docs/decisions/0003-deploy-to-aws.md` chose App Runner for one reason: it runs
the existing container image unmodified. That reason is now weaker on both
sides. The 2026-09-02 correction to that decision already conceded that
Upstash's one-database-per-account limit forced Redis onto ElastiCache, which
forced a VPC connector, which forced a NAT Gateway — most of App Runner's
simplicity was already gone. Meanwhile the client wants the infrastructure in
their own account, where an EC2 instance is a far more legible and transferable
artifact than a managed service with a VPC connector. EC2 also lets the process
model shed the Render-shaped compromises described in §2, which App Runner —
running one container image — cannot.

## 10. Deliberate non-goals

- **Autoscaling and multi-AZ.** One instance, one AZ. There is no traffic data
  yet (`phases/phase-13-load-soak-tuning.md` remains unstarted), and a
  single-instance design keeps Celery beat unambiguously singular. Revisit with
  real numbers, not in advance.
- **Prefork Celery.** Staying on `--pool=solo`. Concurrency was already 1, so
  prefork buys nothing until there is load to justify it, and solo sidesteps
  the closed-event-loop class of bug that has appeared three times in this
  codebase.
- **Merging V1 and V2.** They remain separate services with separate images and
  separate Redis databases. V1 stays untouched apart from the storage adapter
  and its `REDIS_URL`.
- **Moving Google Sheets config or Gemini.** Out of scope; both stay.
- **Infrastructure as code.** Provisioning is scripted bash
  (`scripts/aws_provision.sh` exists in draft) rather than Terraform. One
  instance, provisioned once, in someone else's account: Terraform's state
  management is a larger commitment than the problem warrants. Revisit if the
  client wants to own repeatable environments.

## 11. Open items

- **Region.** Client's decision. Compute, RDS and S3 must share it.
- **Domain and subdomains.** Client-supplied; needed before step 3 of cutover,
  not before implementation starts.
- **AWS account access.** An IAM role for the work, a GitHub OIDC provider, and
  the deploy role's trust policy — all in the client's account. Stage C cannot
  start without these; Stages A and B need only S3 and RDS access.
- **Supabase Postgres major version**, to match the RDS engine version.
- **Retention of the Supabase dump** after teardown — client's call.

## 12. Repository state note

`~/jewelry-api` currently has uncommitted work from the App Runner attempt:
modified `docker-compose.yml`, `docs/deployment-aws.md`,
`docs/decisions/0003-deploy-to-aws.md`, and untracked
`docs/deployment-aws-runbook.md`, `docs/incident-2026-08-25-recolor-mix-oom.md`,
`scripts/aws_provision.sh`. These need triage before implementation — the
incident document is worth keeping as-is, and `aws_provision.sh` is a useful
starting point for Stage C, but the App Runner-specific parts of the deployment
docs are now superseded by this design.
