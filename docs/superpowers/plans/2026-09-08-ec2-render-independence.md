# EC2 Render-Independence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `jewelry-api` (V2) and `jewellery-gen-backend` (V1) off Render
onto one EC2 instance via Docker Compose, replacing only V2's Render-managed
Redis, with zero application code changes.

**Architecture:** Each repo gets a `docker-compose.prod.yml` overlay — the
existing dev `docker-compose.yml` is never edited, so a developer's plain
`docker compose up` is unaffected. A separate runbook document carries every
step that has to run on AWS or on the EC2 instance itself, since neither can
be done by the assistant directly (no AWS credentials, no SSH access) — those
steps are executed by the user, who reports results back.

**Tech Stack:** Docker Compose, existing Dockerfiles (no changes), Ubuntu
22.04 EC2 instance.

**Spec:** `docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`

## Global Constraints

- **No application code changes anywhere in either repo.**
- Existing `docker-compose.yml` in both repos stays byte-for-byte unmodified —
  every production difference lives in a new `docker-compose.prod.yml`.
- V1's Redis (Upstash) is untouched. Only V2's Render-managed Redis is
  replaced with a container.
- V2 `IO_QUEUE_CONCURRENCY` is pinned to `2` **in the worker's command
  override itself**, not via an env var default — the base file's
  `-c ${IO_QUEUE_CONCURRENCY:-20}` must never be reachable in prod regardless
  of what `.env` does or doesn't set.
- A `migrate` one-shot service runs `alembic upgrade head` before `api`,
  `worker`, and `beat` start (`depends_on: {condition: service_completed_successfully}`).
- **Corrected during Tasks 1 and 3 execution (was wrong when this plan was
  written):** Compose merges `depends_on` and `environment` (map-type fields)
  by key union — a later file's key overrides the same key in an earlier
  file, and new keys merge in without needing to restate existing ones.
  **List-type fields (`ports`, `volumes`) merge by concatenation, not
  replacement** — an overlay's `ports: []` does *not* clear a base file's
  published ports. Clearing to empty needs the explicit `!reset` YAML tag
  (`ports: !reset []`, `depends_on: !reset {}`). **Replacing with a genuinely
  new, non-empty value needs `!override` instead — `!reset` combined with a
  non-empty value discards the value too and clears the field to nothing.**
  Both confirmed by testing directly against Docker Compose v5.2.0 (Task 1
  found the clear-to-empty case; Task 3 found the replace-with-a-value case
  when V1's port remap silently produced no published port at all under
  `!reset`). See the ledger for both. This governs the redis-port fix below
  and the V1 fixes two bullets down.
- **V1's base `docker-compose.yml` hardcodes `environment: REDIS_URL:
  redis://redis:6379/0` on both `api` and `worker`, and both declare
  `depends_on: {redis: {condition: service_healthy}}`.** Confirmed by running
  `docker compose config` against the unmodified file (this session, prior to
  writing this plan) — the environment override wins regardless of `.env`
  content, and the dependency means `redis` starts automatically even when
  not named on the `up` command line. **Both must be overridden in the V1
  prod overlay, and the depends_on override must use `!reset` per the
  correction above** — this was not caught when the spec was written and is
  corrected as part of Task 3, with the spec file itself updated to record it.
- V1's `.gitignore` excludes both `.env` and `.env.*` — no new tracked file in
  that repo may start with `.env.`, or it silently never gets committed.
- Region: `ap-south-1`. Ports: V2 api → host `8000`, V1 api → host `8001`.
  Neither repo's Redis publishes a host port in production.
- Security group (documented in the runbook, not repo code): `22` from the
  operator's IP only, `8000`/`8001` from `0.0.0.0/0`, no inbound `6379`.

---

## File Structure

| File | Repo | Responsibility |
| :--- | :--- | :--- |
| `docker-compose.prod.yml` | `~/jewelry-api` | Prod overlay: `migrate` service, pinned worker concurrency, no Redis host port, restart policies |
| `docs/deployment-ec2.md` | `~/jewelry-api` | What runs where, env var deltas from Render, how to deploy an update, how to roll back |
| `docker-compose.prod.yml` | `~/Claude/Projects/jewellery-gen-backend` | Prod overlay: drop `--reload`/volumes, fix the `REDIS_URL`/`depends_on` traps above, remap port to 8001, restart policies |
| `docs/deployment-ec2.md` | `~/Claude/Projects/jewellery-gen-backend` | Same shape as V2's, for V1 |
| `docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md` | `~/jewelry-api` | Modify: append the V1 `REDIS_URL`/`depends_on` correction found in Task 3 |
| `docs/ec2-cutover-runbook.md` | `~/jewelry-api` | The AWS-console-and-EC2-shell runbook the user executes themselves |

---

## Task 1: V2 — `docker-compose.prod.yml` overlay

**Files:**
- Create: `~/jewelry-api/docker-compose.prod.yml`

**Interfaces:**
- Consumes: `~/jewelry-api/docker-compose.yml` (unmodified base — `redis`,
  `api`, `worker`, `beat` services, all `build: .`, all `env_file: .env`).
- Produces: a merged config (via `docker compose -f docker-compose.yml -f
  docker-compose.prod.yml config`) with a fifth `migrate` service that every
  other service depends on for successful completion, `worker`'s command
  hardcoded to `-c 2`, `redis` publishing no host port, and `restart:
  unless-stopped` on all four long-running services.

- [ ] **Step 1: Write the overlay file**

```yaml
services:
  migrate:
    build: .
    env_file: .env
    depends_on:
      redis:
        condition: service_started
    command: alembic upgrade head
    restart: "no"

  redis:
    restart: unless-stopped
    ports: !reset []

  api:
    restart: unless-stopped
    depends_on:
      redis:
        condition: service_started
      migrate:
        condition: service_completed_successfully

  worker:
    restart: unless-stopped
    command: celery -A app.workers.celery_app worker -Q io -c 2 --hostname=worker@%h
    depends_on:
      redis:
        condition: service_started
      migrate:
        condition: service_completed_successfully

  beat:
    restart: unless-stopped
    depends_on:
      redis:
        condition: service_started
      migrate:
        condition: service_completed_successfully
```

- [ ] **Step 2: Validate the merge locally**

```bash
cd ~/jewelry-api
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml config > /tmp/v2-merged.yml
```

Expected: no error. `docker compose config` fully resolves the merge without
needing a running daemon or real secrets — `.env.example`'s placeholder
values are enough to satisfy `env_file:` existence checks.

- [ ] **Step 3: Check the merge did what it's supposed to**

```bash
python3 -c "
import yaml
d = yaml.safe_load(open('/tmp/v2-merged.yml'))
assert 'migrate' in d['services'], 'migrate service missing from merge'
assert d['services']['worker']['command'].endswith('-c 2 --hostname=worker@%h'), \
    f\"worker command not pinned: {d['services']['worker']['command']}\"
assert d['services']['redis'].get('ports', []) == [], \
    f\"redis still publishes a port: {d['services']['redis'].get('ports')}\"
for svc in ('api', 'worker', 'beat'):
    deps = d['services'][svc]['depends_on']
    assert 'redis' in deps, f'{svc} lost its redis dependency on merge'
    assert 'migrate' in deps, f'{svc} is missing the migrate dependency'
    assert deps['migrate']['condition'] == 'service_completed_successfully'
print('OK: migrate present, worker pinned to -c 2, redis has no ports, ' \\
      'api/worker/beat depend on both redis and migrate')
"
```

Expected: `OK: ...` printed, no `AssertionError`.

- [ ] **Step 4: Clean up the local test artifacts**

```bash
rm -f .env /tmp/v2-merged.yml
```

`.env` is gitignored in this repo (`.gitignore:4`) so this is a courtesy
cleanup, not a git-safety step — but leaving a stray `.env.example`-derived
file around invites confusion later, so remove it now.

- [ ] **Step 5: Commit**

```bash
cd ~/jewelry-api
git add docker-compose.prod.yml
git commit -m "$(cat <<'EOF'
feat(deploy): add EC2 production overlay for docker-compose

Adds a migrate one-shot service (alembic upgrade head), pins Celery
worker concurrency to 2 regardless of IO_QUEUE_CONCURRENCY (the base
file's -c ${IO_QUEUE_CONCURRENCY:-20} would OOM a t3.small instance at
Render's own measured ~154MB/prefork-child), and stops Redis publishing
a host port. The base docker-compose.yml is untouched.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: V2 — `docs/deployment-ec2.md`

**Files:**
- Create: `~/jewelry-api/docs/deployment-ec2.md`

**Interfaces:**
- Consumes: Task 1's `docker-compose.prod.yml`; the env var list in
  `~/jewelry-api/.env.example`.
- Produces: the reference doc `docs/ec2-cutover-runbook.md` (Task 6) points
  to for V2's exact env var deltas.

- [ ] **Step 1: Write the doc**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
cd ~/jewelry-api
git add docs/deployment-ec2.md
git commit -m "$(cat <<'EOF'
docs: add EC2 deployment reference for jewelry-api

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: V1 — `docker-compose.prod.yml` overlay (with the REDIS_URL/depends_on fix)

**Files:**
- Create: `~/Claude/Projects/jewellery-gen-backend/docker-compose.prod.yml`
- Modify: `~/jewelry-api/docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`

**Interfaces:**
- Consumes: `~/Claude/Projects/jewellery-gen-backend/docker-compose.yml`
  (unmodified base — `redis`, `api`, `worker` services; `api`/`worker` both
  hardcode `environment: REDIS_URL: redis://redis:6379/0` and both declare
  `depends_on: {redis: {condition: service_healthy}}`).
- Produces: a merged config where `api`/`worker` read the real Upstash
  `REDIS_URL` from `.env`, neither depends on (or auto-starts) the base
  file's `redis` service, `api` serves on host port `8001` without
  `--reload` or host-mounted source, and both have `restart: unless-stopped`.

**Why this task exists as written.** Before writing this plan, running
`docker compose -f docker-compose.yml config` against the unmodified V1 repo
confirmed two things the design spec did not catch:

1. `api.environment.REDIS_URL` resolves to the literal `redis://redis:6379/0`
   regardless of what `.env` sets, because Compose's `environment:` mapping
   for a service always wins over that service's own `env_file:` for the
   same key. In production there is no `redis` container for V1 (V1 keeps
   Upstash), so without a fix this is a hard connection failure on the first
   request.
2. `api.depends_on` and `worker.depends_on` both name `redis` with
   `condition: service_healthy`. Compose starts every dependency of a
   service you bring up, whether or not you named that dependency on the
   command line — so `docker compose up -d api worker` would start the local
   `redis` container anyway, silently reintroducing the exact Render-Redis
   footprint this whole effort exists to remove (just self-hosted instead of
   Render-hosted, and now with two Redis instances for V1 instead of zero).

- [ ] **Step 1: Write the overlay file**

```yaml
services:
  api:
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    volumes: !reset []
    environment:
      REDIS_URL: ${REDIS_URL}
    depends_on: !reset {}
    ports: !override
      - "8001:8000"
    restart: unless-stopped

  worker:
    volumes: !reset []
    environment:
      REDIS_URL: ${REDIS_URL}
    depends_on: !reset {}
    restart: unless-stopped
```

**`!override` vs `!reset`, precisely — found while implementing this task.**
`!reset` clears a field to empty/null and discards anything written alongside
it; testing `ports: !reset` followed by a non-empty list produced **no ports
at all**, not the new value. `!override` is the separate tag that replaces a
base field with a genuinely new, non-empty value. So: clearing a list/map to
nothing (`volumes`, `depends_on` here) uses `!reset`; replacing one with a
different non-empty value (`ports`, remapped from `8000:8000` to
`8001:8000`) uses `!override`. Using `!reset` for the port remap here would
have silently produced a container with no published port at all — a real
found-in-implementation bug, not a hypothetical.

`environment.REDIS_URL: ${REDIS_URL}` works because Compose interpolates
`${VAR}` in a compose file from the project's own `.env` file at parse time —
the same `.env` file each service's `env_file:` directive loads into the
container at runtime. So this line takes whatever real Upstash URL is in
`.env` and uses it to override the base file's hardcoded local value, rather
than introducing a second source of truth for it.

`depends_on: !reset {}` on both services means neither declares a dependency on
`redis` any more, so `docker compose up -d api worker` (Task 6's runbook
always names services explicitly for this repo, as a second, independent
safeguard) never starts it.

- [ ] **Step 2: Validate the merge catches both original bugs**

```bash
cd ~/Claude/Projects/jewellery-gen-backend
cp .env.example .env
# Prove the fix actually reads from .env, not just replaces one hardcode with another
sed -i.bak 's#^REDIS_URL=.*#REDIS_URL=redis://default:test-upstash-token@example.upstash.io:6379#' .env
docker compose -f docker-compose.yml -f docker-compose.prod.yml config > /tmp/v1-merged.yml
```

- [ ] **Step 3: Check the merge did what it's supposed to**

```bash
python3 -c "
import yaml
d = yaml.safe_load(open('/tmp/v1-merged.yml'))
for svc in ('api', 'worker'):
    env = d['services'][svc]['environment']
    assert env.get('REDIS_URL') == 'redis://default:test-upstash-token@example.upstash.io:6379', \
        f'{svc} REDIS_URL did not pick up the real value: {env.get(\"REDIS_URL\")}'
    deps = d['services'][svc].get('depends_on') or {}
    assert 'redis' not in deps, f'{svc} still depends on redis: {deps}'
assert d['services']['api']['ports'] == ['8001:8000'], \
    f\"api port not remapped: {d['services']['api']['ports']}\"
assert '--reload' not in d['services']['api']['command'], \
    'api command still has --reload'
assert d['services']['api'].get('volumes', []) == [], \
    f\"api still bind-mounts source: {d['services']['api'].get('volumes')}\"
print('OK: REDIS_URL reads through from .env, neither service depends on ' \\
      'redis, api serves on 8001 without --reload or host-mounted source')
"
```

Expected: `OK: ...` printed, no `AssertionError`.

- [ ] **Step 4: Clean up local test artifacts**

```bash
rm -f .env .env.bak /tmp/v1-merged.yml
```

- [ ] **Step 5: Record the correction in the design spec**

Open `~/jewelry-api/docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`
and append a dated correction after the "### V1 (`jewellery-gen-backend`) —
new `docker-compose.prod.yml`" table:

```markdown
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
```

- [ ] **Step 6: Commit both files together**

```bash
cd ~/Claude/Projects/jewellery-gen-backend
git add docker-compose.prod.yml
git commit -m "$(cat <<'EOF'
feat(deploy): add EC2 production overlay for docker-compose

Fixes two things the base compose file would otherwise carry into
production unmodified: api/worker's hardcoded REDIS_URL (which always
wins over env_file for the same key, and would point at a local redis
container that doesn't exist here — V1 keeps its existing Upstash
Redis) and their depends_on: redis declaration (which starts redis as
a dependency regardless of whether it's named on the up command line).
Also drops --reload and the host source bind-mounts for production,
and remaps the port to 8001 so it doesn't collide with jewelry-api on
the same host.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"

cd ~/jewelry-api
git add docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md
git commit -m "$(cat <<'EOF'
docs: correct the V1 overlay design — REDIS_URL and depends_on both needed explicit fixes

Found while writing the implementation plan, by actually running
docker compose config against the unmodified V1 repo rather than
reading the compose file by eye.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: V1 — `docs/deployment-ec2.md`

**Files:**
- Create: `~/Claude/Projects/jewellery-gen-backend/docs/deployment-ec2.md`

**Interfaces:**
- Consumes: Task 3's `docker-compose.prod.yml`; the env var list in
  `~/Claude/Projects/jewellery-gen-backend/.env.example`.
- Produces: the reference doc `docs/ec2-cutover-runbook.md` (Task 6) points
  to for V1's exact env var deltas.

- [ ] **Step 1: Write the doc**

```markdown
# EC2 Deployment (Render Independence)

See `~/jewelry-api/docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`
for the full design. This doc is the quick reference for running this
service on the shared EC2 instance instead of Render.

`docs/deployment-free-tier.md` (Render) stays documented as a fallback path.

## What runs

Two containers, named explicitly on every command — **never run a bare
`docker compose up -d`** in this repo on this host, since the base
`docker-compose.yml` also defines a `redis` service this deployment
deliberately does not use (this repo's Redis is Upstash, unchanged from
Render):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api worker
```

## Env vars — deltas from the Render dashboard only

Copy every value from Render's Environment tab for this service into this
host's `.env` unchanged, **except**:

| Variable | Render value | EC2 value |
| :--- | :--- | :--- |
| `WORKER_IN_PROCESS` | `true` | `false` |

`REDIS_URL` keeps its existing Upstash value — copy it across unchanged.
Confirm this before deploying: open this service's Render Environment tab
and check `REDIS_URL` actually points at Upstash, not a Render-managed
instance. If it points at Render instead, this deployment needs a local
Redis container added back and this doc is wrong for that case — stop and
re-check the design spec's own note on this.

Everything else (`API_KEYS`, `ADMIN_API_KEY`, `GOOGLE_SHEET_ID`,
`GOOGLE_SERVICE_ACCOUNT_JSON`, `SUPABASE_*`, `STORAGE_BACKEND`,
`GEMINI_API_KEY`, `PROVIDER`, `CORS_ALLOWED_ORIGINS`, rate/quota limits)
carries across verbatim — see `.env.example` for the full list.

## Deploying an update

```bash
cd /opt/jewelry/jewellery-gen-backend
git pull origin master
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build api worker
```

## Rolling back to Render

Render was never deleted — only suspended. Resume the service from the
Render dashboard and repoint whatever calls this API back at the Render URL.
```

- [ ] **Step 2: Commit**

```bash
cd ~/Claude/Projects/jewellery-gen-backend
git add docs/deployment-ec2.md
git commit -m "$(cat <<'EOF'
docs: add EC2 deployment reference for jewellery-gen-backend

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `.env` naming guard for V1

**Files:**
- None created — this task is a verification-only guard, folded in here
  rather than into Task 3/4 because it checks something about the *repo's
  own gitignore behavior* that neither of those tasks' deliverables directly
  exercise.

**Interfaces:**
- Consumes: `~/Claude/Projects/jewellery-gen-backend/.gitignore` (contains
  both `.env` and `.env.*`).
- Produces: confirmation that no file this plan created accidentally matches
  that pattern and silently failed to be tracked.

- [ ] **Step 1: Confirm both new V1 files are actually tracked**

```bash
cd ~/Claude/Projects/jewellery-gen-backend
git ls-files | grep -E "docker-compose\.prod\.yml|docs/deployment-ec2\.md"
```

Expected: both paths printed. If either is missing, it matched
`.gitignore`'s `.env.*` pattern or another rule and was never staged —
neither filename here starts with `.env.`, so this should pass, but it's
worth a real check rather than an assumption given `.gitignore`'s pattern is
exactly what made the original `.env.production.example` naming considered
during design (and rejected) a real trap.

---

## Task 6: EC2 cutover runbook

**Files:**
- Create: `~/jewelry-api/docs/ec2-cutover-runbook.md`

**Interfaces:**
- Consumes: Tasks 1-4's `docker-compose.prod.yml` and `docs/deployment-ec2.md`
  in both repos; the spec's instance sizing (`t3.small`, Ubuntu 22.04, 20GB
  gp3, Elastic IP), security group rules, and region (`ap-south-1`).
- Produces: nothing further in this plan — this is the last artifact. Every
  step in it is executed by the user, not the assistant, since neither AWS
  console access nor SSH access to the resulting instance is available here.

- [ ] **Step 1: Write the runbook**

```markdown
# EC2 Cutover Runbook

Execute this yourself in the AWS console and over SSH — this session has no
AWS credentials and cannot reach the instance directly. Report back what you
see at each checkpoint (marked **CHECK**) before moving to the next section.

See `docs/superpowers/specs/2026-09-08-stage-c-render-independence-design.md`
for why each choice below is what it is.

## 1. Launch the instance

AWS Console → EC2 → Launch instance, region **ap-south-1**:

| Field | Value |
| :--- | :--- |
| Name | `jewelry-render-independence` |
| AMI | Ubuntu Server 22.04 LTS (64-bit x86) |
| Instance type | `t3.small` |
| Key pair | create new, download the `.pem`, keep it — you'll SSH with it |
| Network settings → Create security group | see table below |
| Storage | 20 GiB gp3 (default is usually fine, just confirm it's gp3) |

Security group rules:

| Type | Port | Source |
| :--- | :--- | :--- |
| SSH | 22 | My IP (not `0.0.0.0/0`) |
| Custom TCP | 8000 | Anywhere (`0.0.0.0/0`) |
| Custom TCP | 8001 | Anywhere (`0.0.0.0/0`) |

Do not add a rule for 6379 — Redis stays unreachable from outside the
instance.

Launch it.

## 2. Attach an Elastic IP

EC2 → Elastic IPs → Allocate Elastic IP address → Allocate.
Then Actions → Associate Elastic IP address → select the instance you just
launched.

**CHECK:** note the Elastic IP address here — every step below refers to it
as `<ELASTIC_IP>`.

## 3. SSH in and bootstrap

```bash
ssh -i /path/to/your-key.pem ubuntu@<ELASTIC_IP>
```

Once connected:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu
```

Log out and back in (`exit`, then SSH again) so the `docker` group
membership takes effect without needing `sudo` for every command.

```bash
docker --version
docker compose version
```

**CHECK:** both commands print a version with no error.

## 4. Clone both repos

```bash
sudo mkdir -p /opt/jewelry
sudo chown ubuntu:ubuntu /opt/jewelry
cd /opt/jewelry
git clone https://github.com/animachiii/jewelry-api.git
git clone https://github.com/animachiii/jewellery-gen-backend.git
cd jewelry-api && git checkout main && cd ..
cd jewellery-gen-backend && git checkout master && cd ..
```

(If either repo is private, you'll need a GitHub personal access token or an
SSH deploy key set up on this instance first — same as any other private
clone.)

## 5. Populate `.env` for jewelry-api

```bash
cd /opt/jewelry/jewelry-api
cp .env.example .env
nano .env   # or your editor of choice
```

Follow `docs/deployment-ec2.md` in this repo for the exact delta table.
Open the Render dashboard for `jewelry-api` (Environment tab) in a browser
alongside this and copy every value across, applying the deltas the doc
lists.

**CHECK:** `cat .env` and confirm no line still says
`postgresql+asyncpg://postgres.<ref>:<pw>@<host>:5432/postgres` or another
obvious placeholder from `.env.example` — every value should be real.

## 6. Populate `.env` for jewellery-gen-backend

```bash
cd /opt/jewelry/jewellery-gen-backend
cp .env.example .env
nano .env
```

Same process, following that repo's own `docs/deployment-ec2.md`. Pay
particular attention to `REDIS_URL` — this doc's table says to carry it
across unchanged, but confirm in the Render dashboard that it's actually an
Upstash URL before doing so.

**CHECK:** `cat .env` shows real values, and `REDIS_URL` starts with
something like `redis://default:...@....upstash.io:...` — not
`redis://red-...`(a Render-internal hostname shape) or `localhost`.

## 7. Bring up jewelry-api

```bash
cd /opt/jewelry/jewelry-api
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

**CHECK:** `migrate` shows `Exited (0)`; `api`, `worker`, `beat`, `redis` all
show `Up` (or `Up (healthy)` once Docker's own healthcheck settles). If
`migrate` shows a non-zero exit code, run
`docker compose -f docker-compose.yml -f docker-compose.prod.yml logs migrate`
and report the output before continuing — do not proceed with a failed
migration.

```bash
curl -s localhost:8000/api/v2/health
```

**CHECK:** JSON response with `"status": "ok"` (or `"degraded"` only if you
already know a dependency is down for an unrelated reason — report what you
see either way).

## 8. Bring up jewellery-gen-backend

```bash
cd /opt/jewelry/jewellery-gen-backend
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api worker
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

**CHECK:** only `api` and `worker` are listed and both show `Up` — if a
`redis` container is also listed here, stop and report it; that means the
`depends_on: !reset {}` override in `docker-compose.prod.yml` didn't take effect
and the instance now has an unused local Redis nobody intended to run.

```bash
curl -s localhost:8001/health
```

**CHECK:** `200 OK` response.

## 9. Verify from outside the instance

From your own machine, not the SSH session:

```bash
curl -s http://<ELASTIC_IP>:8000/api/v2/health
curl -s http://<ELASTIC_IP>:8001/health
```

**CHECK:** both reachable and healthy over the public IP.

## 10. Verify the real request path, not just health

Health checks Postgres and Redis directly but **not storage** — V2's health
endpoint hardcodes `storage: "ok"` regardless of what's actually configured
(see the design spec's note on this). So:

```bash
curl -s -H "X-API-Key: <a real client or ops key from the current Render deployment>" \
  http://<ELASTIC_IP>:8000/api/v2/config
```

**CHECK:** returns the real category/angle config, not a 401/500.

Then, open `http://<ELASTIC_IP>:8000/ui` in a browser and submit one real
test job exactly as you would against the Render deployment today (same
`/ui` demo client, same API key). Watch it move off `PENDING` — this
exercises Celery consuming from the new container Redis for the first time.

**CHECK:** the submitted job's status changes away from `PENDING` within a
minute or two. Report the job's final status either way (a real generation
failure due to unrelated causes — e.g. no real `GEMINI_API_KEY` — is a
different problem than the worker never picking up the job at all; tell me
which one you see).

## 11. Reboot test

```bash
sudo reboot
```

Wait about a minute, then SSH back in:

```bash
docker ps
```

**CHECK:** all containers you expect (jewelry-api's `api`/`worker`/`beat`/
`redis`, jewellery-gen-backend's `api`/`worker`) are back up with no manual
`docker compose up` needed. `migrate` will not reappear — it's `restart:
"no"` and only runs once per explicit `up`, which is correct.

## 12. Suspend (not delete) Render

Only after everything above checks out and you're satisfied. In the Render
dashboard:

- Suspend the `jewelry-api` web service
- Suspend the `jewellery-gen-backend` web service
- **Do not** delete `jewelry-api-redis` yet — wait at least 48 hours of the
  EC2 instance behaving correctly before deleting anything on Render. If
  something's wrong, resume the Render services immediately; nothing on
  Render was modified by this migration, so resuming is a full, clean
  rollback.

## Done

Report back: which CHECK points passed, which didn't, and the exact output
for anything that didn't match the expected result. Don't guess past a
failed CHECK — stop there and report it.
```

- [ ] **Step 2: Self-review the runbook for placeholders**

```bash
grep -n "TBD\|TODO\|fill in\|placeholder" ~/jewelry-api/docs/ec2-cutover-runbook.md
```

Expected: no matches other than the literal word "placeholder" appearing
inside CHECK-step prose that's *describing* what a bad value would look like
(e.g. "no line still says ... an obvious placeholder") — read the grep
output and confirm every match is that kind of reference, not an actual gap
left for later.

- [ ] **Step 3: Commit**

```bash
cd ~/jewelry-api
git add docs/ec2-cutover-runbook.md
git commit -m "$(cat <<'EOF'
docs: add the EC2 cutover runbook

Every step here runs on AWS console or over SSH to the new instance —
neither is reachable from this session, so this is written for the
user to execute and report results against, with explicit CHECK
points rather than an assumption that each step worked.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Notes

Checked against the spec:

- Repository changes table (V2 four fixes, V1 four fixes) — Tasks 1 and 3,
  plus the two additional V1 fixes found while writing this plan (recorded
  in the spec correction, Task 3 Step 5).
- Env var deltas tables — Tasks 2 and 4.
- Instance sizing, security group, region — Task 6 Step 1.
- Cutover and rollback sequence, including the "verify beyond health"
  requirement given the storage-check gap — Task 6 Step 1, sections 10-12.
- `.env`/`.env.*` naming trap for V1 — Task 5, and avoided by construction in
  Tasks 3-4 (`docker-compose.prod.yml` and `docs/deployment-ec2.md` don't
  match either pattern).
- App Runner runbook and `scripts/aws_provision.sh` are explicitly not
  touched or reused — confirmed no task references them.

Nothing in the spec's Non-goals section (Task 9 verification, Stage B/RDS,
domain/TLS, CI/CD to EC2) has a task here, which is correct — they're out of
scope by design.
