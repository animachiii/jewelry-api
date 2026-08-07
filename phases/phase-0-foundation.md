# Phase 0 — Foundation & Environment

> **Superseded 2026-08-07:** local matting (Step 5, and the `gpu`/`io` queue
> split it motivated in Steps 1/4) was dropped — see
> `docs/decisions/0001-drop-local-matting.md`. Step 5's checkpoints were
> never run (correctly blocked pending GPU access + client photos) and are
> now moot rather than pending. Steps 1-4/6/7 below are historical record of
> what Phase 0 actually verified at the time — they describe a two-queue
> design that no longer exists in the codebase. Current architecture lives
> in `CLAUDE.md` and `docs/`, not here.

## Objective

Stand up the skeleton every later phase depends on: repo structure, Supabase Postgres with
the full schema migrated, Supabase Storage buckets, Redis, the Celery app with split
GPU/IO queues, the test harness, a CI skeleton, and seed data. Also resolve the one open
decision that blocks the pipeline design — which matting model we are legally and
technically allowed to ship.

No business logic, no API endpoints, no AI pipeline. This phase produces a repo that boots,
connects to everything, migrates cleanly, and runs an empty test suite green.

## Context

Nothing exists yet. This is the first phase.

Prerequisite decisions already made and recorded in `claude.md`:
Supabase for Postgres + Storage, Redis for broker/cache only, Celery with separate `gpu`
and `io` queues, FastAPI + Pydantic v2.

**Split note:** this phase is deliberately two-tracked. **Step 5 (the matting benchmark)
has no code dependency on Steps 1–4** and should be run in parallel by whoever has GPU
access — it gates Phase 5, not Phase 1. If the benchmark can't start immediately, do not
let it block the rest of Phase 0.

---

## Step 1 — Repository scaffold and tooling

### What to do

Create the folder structure exactly as documented in `claude.md`. Every package directory
gets an `__init__.py`; every leaf module listed in the structure gets created, even if it
only contains a docstring and a `TODO`. An empty-but-present file is cheaper than a later
argument about where something goes.

Set up dependency management with `uv` (or Poetry — pick one and record it). Pin Python
3.12.

Install and configure:

- `ruff` — lint + format, config in `pyproject.toml`
- `mypy --strict` scoped to `app/`
- `pre-commit` running both on staged files
- `pytest`, `pytest-asyncio`, `pytest-cov`

Create `app/config.py` with a Pydantic `Settings` class covering every variable in
`.env.example` below. This is the **only** place in the codebase that reads the
environment — see @docs/conventions.md.

Write `.env.example`, committed, every variable commented. Gitignore `.env`.

```
# --- Application ---
APP_ENV=local                      # local | staging | production
LOG_LEVEL=INFO
API_BASE_PATH=/api/v2

# --- Supabase Postgres ---
# Session pooler (5432), NOT transaction pooler (6543) — SQLAlchemy uses prepared statements
DATABASE_URL=postgresql+asyncpg://postgres.<ref>:<pw>@<host>:5432/postgres

# --- Supabase Storage ---
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SERVICE_KEY=              # service role key — never expose to any client
BUCKET_INPUTS=jewelry-inputs
BUCKET_MATTES=jewelry-mattes
BUCKET_OUTPUTS=jewelry-outputs
SIGNED_URL_TTL_SECONDS=3600

# --- Redis ---
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2

# --- Celery / workers ---
GPU_QUEUE_CONCURRENCY=1            # set from measured VRAM in Step 5
IO_QUEUE_CONCURRENCY=20

# --- Models (pinned, never floating aliases) ---
MATTING_MODEL_ID=ZhengPeng7/BiRefNet-matting
MATTING_MODEL_REVISION=            # pin the commit hash
QA_MODEL_ID=

# --- Google Sheets ---
GOOGLE_SERVICE_ACCOUNT_JSON=       # path or inline JSON
CONFIG_SHEET_ID=
CONFIG_SYNC_CRON=*/15 * * * *

# --- Gemini ---
GEMINI_API_KEY=
GEMINI_RATE_LIMIT_PER_MINUTE=60

# --- Observability ---
SENTRY_DSN=
```

Write `docker-compose.yml` with four services sharing one base image: `api`,
`worker-gpu`, `worker-io`, `beat`, plus local `redis`. Postgres is **not** a compose
service — it's Supabase. Use a Supabase local branch or a dedicated dev project.

### Checkpoint 1

- [ ] `uv sync` (or `poetry install`) completes from a clean checkout with no manual steps
- [ ] `ruff check app/ tests/` exits 0
- [ ] `mypy --strict app/` exits 0
- [ ] `pytest` runs and reports 0 failures (0 tests is acceptable at this point)
- [ ] `python -c "from app.config import settings; print(settings.APP_ENV)"` prints `local`
- [ ] `grep -rn "os.getenv" app/ --include=*.py` returns only `app/config.py`
- [ ] `docker compose config` validates and lists exactly: `api`, `worker-gpu`, `worker-io`, `beat`, `redis`
- [ ] `.env` is gitignored; `.env.example` is committed and contains every field above

---

## Step 2 — Supabase Postgres and full schema migration

### What to do

Create the Supabase project (or a branch on an existing one). Record the **session pooler**
connection string — port 5432, not 6543. The transaction pooler does not support prepared
statements and SQLAlchemy will fail in non-obvious ways.

Initialize Alembic against `DATABASE_URL`. Configure it for async (`asyncpg`).

Write migration `0001_initial_schema` creating **every** enum, table, index, and constraint
in @docs/schema.md. Do not defer tables to later phases — the schema is designed as a whole
and partial migration produces contradictory foreign keys. Specifically:

Enums: `angle_t`, `job_status_t`, `sub_job_status_t`, `source_type_t`, `asset_kind_t`,
`failure_class_t`, `qa_status_t`, `sync_status_t`.

Tables: `api_clients`, `config_versions`, `jobs`, `sub_jobs`, `assets`, `cost_events`,
`job_events`.

Do not forget these three, they are easy to miss and expensive to add later:

- Partial unique index on `config_versions (is_active) WHERE is_active` — enforces
  exactly one active config version at the database level, not in application code
- Unique constraint on `jobs (client_id, idempotency_key)`
- Unique constraint on `sub_jobs (job_id, angle)`

Write the SQLAlchemy 2.0 ORM models in `app/db/models/` using typed `Mapped[]` columns,
matching the migration exactly. Set up `app/db/session.py` with an async engine and a
session factory.

RLS stays disabled — the backend is the only writer and connects as service role. Note this
explicitly in the migration comment so nobody "fixes" it later.

### Checkpoint 2

- [ ] `alembic upgrade head` applies cleanly against an empty database
- [ ] `alembic downgrade base` then `alembic upgrade head` succeeds — proves reversibility
- [ ] All 7 tables exist: verified via `\dt` or an information_schema query
- [ ] All 8 enums exist with exactly the values in @docs/schema.md
- [ ] Inserting a second `config_versions` row with `is_active = true` raises a unique
      violation
- [ ] Inserting a duplicate `(client_id, idempotency_key)` into `jobs` raises a unique violation
- [ ] Inserting a duplicate `(job_id, angle)` into `sub_jobs` raises a unique violation
- [ ] Deleting a `jobs` row cascades to its `sub_jobs` rows
- [ ] `alembic check` reports no drift between ORM models and the migration
- [ ] An async session can `SELECT 1` through `app/db/session.py`

---

## Step 3 — Supabase Storage buckets and storage service

### What to do

Create three **private** buckets: `jewelry-inputs`, `jewelry-mattes`, `jewelry-outputs`.
None are public — there is no public read path in this system.

Implement `app/services/storage_service.py` with: presigned upload URL generation, download
to a temp path, upload from a temp path, signed read URL generation, and existence check.

Path convention, enforced by a helper rather than string-formatted at call sites:
`{job_id}/{angle}/{kind}_{short_uuid}.{ext}`

Configure lifecycle rules per @docs/business-rules.md §11 — inputs 90 days, mattes 30 days,
outputs indefinite. If Supabase lifecycle rules aren't available on the current plan,
implement a Celery beat cleanup task reading `assets.expires_at` and note the deviation.

**Watch the egress.** Supabase Storage bills egress. The Flutter ERP re-pulling catalog
images on every status poll is a real cost line. Signed URLs with a 1-hour TTL let the
client cache; make sure the ERP team knows to cache rather than re-poll for the URL.

### Checkpoint 3

- [ ] All three buckets exist and are private — an unauthenticated GET to a known object
      path returns 400/403, not the image
- [ ] `storage_service.generate_upload_url()` returns a URL that accepts a real PUT of a
      test image
- [ ] The uploaded object is retrievable via `storage_service.generate_signed_url()`
- [ ] A signed URL returns 403 after its TTL expires (test with a 5-second TTL)
- [ ] Round trip works: upload a known file, download it, checksums match
- [ ] The path helper produces `{job_id}/{angle}/{kind}_{uuid}.png` for all three kinds
- [ ] `grep -rn "SUPABASE_SERVICE_KEY" app/` shows it referenced only in `config.py` and
      `storage_service.py`

---

## Step 4 — Redis, Celery, and split queues

### What to do

Configure `app/workers/celery_app.py` with Redis broker and result backend on **separate
logical databases** from the application cache (see `.env.example` — db 0/1/2).

Define two queues with explicit task routing:

| Queue | Workload | Concurrency | Pool |
| :--- | :--- | :--- | :--- |
| `gpu` | Matting | `GPU_QUEUE_CONCURRENCY` (1–2) | prefork |
| `io` | Gemini calls, QA, config sync | `IO_QUEUE_CONCURRENCY` (20) | prefork or gevent |

Routing is declared in `task_routes`, not decided at call time. A task that ends up on the
wrong queue is the failure mode this whole split exists to prevent.

Add the `worker_process_init` signal handler in `app/workers/matting.py` — for now it logs
"model would load here" and sets a module-level singleton to a sentinel. The real model
loads in Phase 5, but **the loading location is established now** so nobody later loads it
inside a task body.

Add two trivial tasks for verification: `health.ping_gpu` routed to `gpu`,
`health.ping_io` routed to `io`. Each returns its queue name and PID.

Set up Redis helpers in `app/core/`: `ratelimit.py` (token bucket) and `idempotency.py`
(key → job_id with 24h TTL). Implementations can be thin; the interfaces are what Phase 2
and Phase 10 build on.

Configure Celery beat with the config sync schedule from `CONFIG_SYNC_CRON` — the task
itself is a stub until Phase 3.

### Checkpoint 4

- [ ] `celery -A app.workers.celery_app inspect ping` responds from both workers
- [ ] `health.ping_gpu` executes **only** on `worker-gpu`; verified via the returned PID
      matching the gpu container
- [ ] `health.ping_io` executes **only** on `worker-io`
- [ ] `celery -A app.workers.celery_app inspect active_queues` shows `worker-gpu` consuming
      only `gpu` and `worker-io` consuming only `io`
- [ ] The `worker_process_init` handler fires **once per worker process** at boot — verified
      by log lines equal to the configured concurrency, not per task
- [ ] `flushall` on Redis followed by a `ping_io` still succeeds — nothing depends on
      persisted Redis state
- [ ] Token bucket allows N requests and rejects N+1 within the window
- [ ] Idempotency helper stores and retrieves a key, and the key expires after its TTL
- [ ] `celery -A app.workers.celery_app inspect scheduled` shows the config sync beat entry

---

## Step 5 — Matting model benchmark and licensing decision

**Run in parallel with Steps 1–4. This gates Phase 5, not Phase 1.**

### What to do

This step exists because the approved v2 spec names `briaai/RMBG-2.0`, which ships under
**CC BY-NC 4.0**. This is a commercial project producing commercial catalog imagery.
Shipping RMBG-2.0 without a paid Bria agreement is a licensing violation, and it is far
cheaper to resolve now than after the pipeline is built around it.

Assemble a benchmark set of **at least 30 real client pieces**, and it must include:

- ≥3 fine chains (the classic matting failure — links a few pixels wide)
- ≥3 transparent or translucent gemstones
- ≥3 high-polish metal surfaces with strong specular highlights
- ≥2 pieces shot against a light/low-contrast background
- ≥2 multi-material pieces (metal + stone + pearl)

Run both models over the set:

- `ZhengPeng7/BiRefNet-matting` — **MIT license**
- `briaai/RMBG-2.0` — CC BY-NC 4.0, evaluation only

Score each output. Do not rely on a single automated metric — a human comparison by
someone who knows what a good jewelry cutout looks like is the deciding input. Record for
each: subjective quality 1–5, whether fine chain detail survived, whether transparency was
handled or flattened, and per-image latency.

Measure VRAM at concurrency 1, 2, and 4 for the chosen model. **Set
`GPU_QUEUE_CONCURRENCY` from this measurement**, not from a guess. This number is the one
that prevents OOM in production.

Write the results and the decision to `docs/decisions/0001-matting-model.md`, including the
licensing rationale so the reasoning survives staff turnover.

**Decision rule:** default to BiRefNet-matting (MIT) — the licensing problem simply
disappears. Only escalate to buying a Bria commercial license if BiRefNet shows a clear,
demonstrated quality gap on the fine-chain and transparency cases, and the client agrees to
the cost. Note that RMBG-2.0 is itself built on the BiRefNet architecture, so a large gap
would be surprising.

### Checkpoint 5

- [ ] Benchmark set assembled with ≥30 real client pieces meeting the category requirements above
- [ ] Both models run over the full set; per-image outputs saved for side-by-side comparison
- [ ] Scoring sheet complete: quality, chain detail, transparency, latency per image per model
- [ ] Peak VRAM recorded for the chosen model at concurrency 1, 2, and 4
- [ ] `GPU_QUEUE_CONCURRENCY` set in `.env.example` from the measured value with a comment
      naming the VRAM headroom it assumes
- [ ] `MATTING_MODEL_ID` and `MATTING_MODEL_REVISION` pinned to a specific commit hash, not
      a branch name
- [ ] `docs/decisions/0001-matting-model.md` written, stating the choice, the quality
      evidence, and the licensing rationale
- [ ] If RMBG-2.0 was chosen: written confirmation the client has purchased a Bria
      commercial license, referenced in the decision doc

---

## Step 6 — Seed data and test harness

### What to do

Write `scripts/seed_dev.py`, idempotent and safe to re-run, producing:

**`api_clients` — 3 rows**

| Name | Scope | Purpose |
| :--- | :--- | :--- |
| Flutter ERP — dev | `client` | Normal path |
| Ops console — dev | `ops` | Ops endpoints |
| Revoked client — dev | `client` | `is_active = false`, for auth tests |

Print the raw keys once on generation. Store only the Argon2 hash — see @docs/schema.md.

**`config_versions` — 2 rows**

One active version covering all 7 categories with realistic per-category angle enablement
(not every category enables all four angles — vary it, that's the realistic case). One
older inactive version, so version-pinning behavior is testable.

Include at least one category with `synthetic_allowed: true` on `DIAGONAL` and one with it
`false` everywhere. Validation logic needs both.

**`jobs` + `sub_jobs` — 8 jobs covering every terminal shape**

| Scenario | Why it must exist |
| :--- | :--- |
| 4/4 succeeded | `COMPLETED` baseline |
| 3 succeeded, 1 `FAILED` | The canonical `PARTIAL_SUCCESS` — retryable |
| 3 succeeded, 1 `REJECTED` (safety refusal) | `PARTIAL_SUCCESS`, **not** retryable |
| 0 succeeded, 4 failed | `FAILED` |
| 1 angle only, failed | Must be `FAILED`, **not** `PARTIAL_SUCCESS` |
| 2 requested, 2 skipped, both succeeded | Proves `SKIPPED` is excluded from the math |
| Synthetic angle in `QA_REVIEW` | Parent must stay `PROCESSING` |
| In-flight: 2 done, 2 `GENERATING` | `PROCESSING` |

**`assets`** — rows for every referenced input/matte/output, with at least one row whose
`expires_at` is in the past (retry-on-expired-input must be testable).

**`cost_events`** — attached to succeeded and rejected sub-jobs both, since a refusal is
still billed.

Then build the test harness: `testcontainers` Postgres, `fakeredis`, `httpx.AsyncClient`
app fixture, `task_always_eager` for Celery unit tests, a `@pytest.mark.gpu` marker skipped
when no card is present, and `tests/fixtures/gemini/` with placeholder files for success,
429, 5xx, timeout, safety refusal, and malformed response.

### Checkpoint 6

- [ ] `python scripts/seed_dev.py` runs clean on an empty database
- [ ] Running it a second time does not duplicate rows or error
- [ ] All 8 job scenarios exist and each parent `status` matches the rule in
      @docs/business-rules.md §3 — verified by a test that recomputes and compares
- [ ] The 1-angle failed job is `FAILED`, not `PARTIAL_SUCCESS`
- [ ] The 2-skipped job is `COMPLETED` with `requested_angles = 2`
- [ ] The `QA_REVIEW` job's parent is `PROCESSING`
- [ ] Exactly one `config_versions` row has `is_active = true`
- [ ] At least one `assets` row has `expires_at` in the past
- [ ] `pytest` spins up a throwaway Postgres via testcontainers and tears it down
- [ ] `pytest -m gpu` skips cleanly on a machine with no GPU
- [ ] All 6 Gemini fixture files exist and parse

---

## Step 7 — CI skeleton

### What to do

CI lands **now**, not in Phase 12. A pipeline added after ten phases of code is a pipeline
that spends its first week failing on accumulated debt.

GitHub Actions workflow on every push and PR:

1. `ruff check` and `ruff format --check`
2. `mypy --strict app/`
3. `alembic upgrade head` against a throwaway Postgres service
4. `pytest` with coverage, GPU tests excluded
5. `docker build` of the base image

Branch protection on `main`: all checks must pass. Secrets via GitHub Secrets — never
committed, and CI must **not** hold production Supabase or Gemini credentials.

### Checkpoint 7

- [ ] Workflow triggers on push and PR
- [ ] All five steps run and pass on a clean `main`
- [ ] A deliberately introduced lint error fails the build
- [ ] A deliberately introduced type error fails the build
- [ ] A deliberately failing test fails the build
- [ ] CI never contacts the live Gemini API, the live Sheets API, or production Supabase —
      verified by grepping the workflow for production hostnames
- [ ] Branch protection blocks merge on a red build

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file
2. Test each one: run the command, query the database, inspect the container, check the
   bucket. Do not mark a checkpoint from memory or from "it should work"
3. Return a structured report:

   ```
   ✅ [Checkpoint] — Pass
   ⚠️ [Checkpoint] — Partial: [specific reason]
   ❌ [Checkpoint] — Fail: [specific reason]
   ```

4. Fix all failures and partials before reporting phase complete
5. If anything in this phase changed the schema, routes, or business rules from what's
   documented in `docs/`, update the relevant `docs/*.md` file now — before declaring the
   phase complete. `claude.md` and `docs/` must reflect reality, not the original plan.
   In particular: if the matting benchmark changed the model choice, update
   @docs/ai-integration.md
6. Only say "Phase 0 Complete" when every checkbox is green and docs are in sync

---

## Final Phase 0 Checklist

- [ ] Repo scaffold, tooling, and `Settings` object in place; `.env.example` complete
- [ ] Full schema migrated to Supabase Postgres, reversible, ORM in sync, all three
      critical constraints enforced at the database level
- [ ] Three private Storage buckets live with a working signed-URL round trip
- [ ] Redis + Celery running with genuinely separated `gpu` and `io` queues, and
      process-level model loading established
- [ ] Matting model chosen, benchmarked, pinned, and **licensing resolved in writing**
- [ ] Seed data covers all 8 job scenarios; test harness runs with no live external calls
- [ ] CI green on `main` with branch protection enforced
- [ ] Self-audit passed with all green
- [ ] `docs/` updated to match what was actually built
- [ ] Manual verification done by architect

---

## Open questions to resolve during this phase

These block later phases. Answer them here rather than discovering them mid-build.

1. **The 7 category codes** — confirm exact codes and per-category angle enablement from
   the client's actual sheet. Seed data is currently a placeholder.
2. **GPU host** — RunPod, Lambda, bare metal, or GCP? Drives Phase 12 and the cost model.
3. **Volume** — jobs/day at launch and at peak? Sizes worker counts and Gemini quota.
4. **Output retention** — how long must generated catalog images be kept?
5. **Tenancy** — single-client, or resold later? `api_clients` covers multi-client auth,
   but true multi-tenant isolation would need a `tenant_id` and it is far cheaper now.
6. **v1 history** — does anything from the n8n bot need preserving? Scopes Phase 14.
