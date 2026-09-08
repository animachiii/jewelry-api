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
