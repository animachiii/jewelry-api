# Deployment — Free Tier (Render + Upstash, no card required)

This is an **alternative** to `docs/deployment.md`'s Fly.io path, for a
deploy that needs no credit card anywhere. Read `docs/deployment.md` first
— this file only documents what differs and why. Same reality-check caveat
applies: this session has no Render or Upstash account and cannot create
one — everything below that needs one is the user's to run.

**Proven, not theoretical**: V1 (`animachiii/jewellery-gen-backend`) already
runs this exact combination — Render free Web Service + Upstash Redis — in
production, 19 real deployments as of 2026-08-06. This path adapts V1's own
`docs/deployment-free-tier.md` pattern to V2's Celery-based architecture.

---

## Why Render alone doesn't fit V2's architecture unmodified

Render's free tier gives one always-on-ish **Web Service** for $0 — but no
free **Background Worker** tier (Starter, ~$7/mo, is the cheapest paid
tier for one) and no free managed Redis. V2 (`app/workers/celery_app.py`)
assumes three separate processes: the API, a Celery `worker`, and Celery
`beat`. Fitting that into one free service needs two things solved:

1. **All three processes in one container.** `scripts/render_start.sh`
   runs `alembic upgrade head`, then backgrounds `celery beat` and
   `celery worker -Q io`, then runs `uvicorn` in the foreground — one
   process tree, one Render service. Render bills per *service*, not per
   OS process inside a container, so this is a process-supervision fix at
   the shell level, not an application-code change (unlike V1's
   `WORKER_IN_PROCESS`, which ran ARQ's worker loop as an asyncio task
   inside the same Python process — a different mechanism for a different
   task runner; Celery isn't built to run as an in-process asyncio task the
   way ARQ is, so this repo doesn't attempt that). If any one of the three
   processes dies, `render_start.sh` tears the other two down rather than
   serving traffic with a half-dead stack (e.g. the API up but nothing
   consuming the job queue) — Render then restarts the whole container.
2. **Redis is Upstash's free tier**, not self-hosted — Render's free tier
   has no persistent disk, so a self-hosted Redis container would lose
   Celery's broker/result state on every restart. Upstash's free tier
   (`rediss://` TLS) persists by default. **Correction: a free Upstash
   account allows exactly one free database, not several** (found by the
   user actually signing up — this doc originally assumed otherwise).
   `REDIS_URL`, `CELERY_BROKER_URL`, and `CELERY_RESULT_BACKEND` all point
   at the **same** single Upstash `rediss://` URL — `app/config.py` treats
   them as independent settings, but nothing stops them sharing one
   instance; Celery's broker/result keys and the app's own `idem:`/
   `ratelimit:`/`provider:gemini:tokens:` keys (`docs/schema.md`) are
   namespaced separately and won't collide.

`render.yaml` at the repo root defines the single web service —
`plan: free`, `dockerfilePath: ./Dockerfile` (same image the Fly path
uses; `scripts/render_start.sh` is the default `CMD`, see the Dockerfile).

---

## Tradeoffs this shape accepts — read before relying on it beyond a demo

- **Cold starts / sleep.** Render's free Web Services spin down after
  ~15 minutes with no inbound HTTP traffic and take tens of seconds to wake
  on the next request. Because the worker and beat processes live in the
  *same* container as the API, **queued jobs stop being processed while the
  service is asleep** — a job dispatched via Celery's `.delay()` just sits
  in Upstash until something (a request, a health check) wakes the
  container back up. This mirrors V1's exact tradeoff for the same reason.
- **No horizontal separation.** A crash or memory spike in the worker
  process can take the whole container down (Render restarts it), unlike
  Fly's three independent machines where a worker crash doesn't touch the
  API. `render_start.sh`'s teardown-on-any-death behavior makes this
  explicit rather than leaving a half-dead container running.
- **Upstash free tier limits** — check current numbers before relying on
  this (they change); V1's own docs note 250MB storage / 500K commands per
  month as the free tier shape at the time it was built. `idem:`/
  `retryidem:`/`ratelimit:`/`provider:gemini:tokens:` keys (`docs/schema.md`)
  are small; the command-count cap is more likely to bind under heavy
  `GET /status` polling than storage is.
- **Single point of failure, shared fate** — same caveat V1's doc gives for
  its own Render path.

If any of these stop being acceptable (real ERP traffic, a client
complaint about cold-start latency, hitting Upstash's command cap), that's
the trigger to move to `docs/deployment.md`'s Fly path, not to work around
it here.

## Secrets

Same set as `docs/deployment.md`'s checklist, entered in Render's dashboard
(Environment tab) instead of via `fly secrets set`:

- `REDIS_URL` / `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` — Upstash's
  `rediss://` connection string(s), not Fly's default `redis://localhost`
  placeholders.
- `APP_ENV=production` and `MOCK_MODE=false` are already set in
  `render.yaml` directly (not secret-shaped, safe to commit — same
  reasoning as V1's `WORKER_IN_PROCESS` being committed in its `render.yaml`).
- No GitHub Actions secret is required for this path at all — Render
  deploys automatically on push once the repo is connected via **Render's
  own dashboard** (New → Blueprint), the same native integration V1
  already uses (confirmed via its GitHub deployment history — the `render`
  GitHub App, not a webhook or `.github/workflows/deploy.yml`, which is
  Fly-specific and stays irrelevant to this path).

## First deploy checklist

1. Create a free Upstash Redis database — **only one is available on a
   free account** — copy its `REDIS_URL` (the `rediss://` one) and reuse it
   for all three Redis vars below.
2. Create a Render account (no card required for the free plan), **New →
   Blueprint**, connect this repo — Render reads `render.yaml` and creates
   the one web service.
3. Enter every secret from `docs/deployment.md`'s checklist (using the
   Upstash URLs from step 1 for the three Redis vars) in the service's
   Environment tab.
4. Deploy. Hit `/api/v2/health` on the assigned `onrender.com` URL —
   expect `{"status": "ok", ...}` (allow for a cold-start delay on the
   first hit).
5. Submit a real `/generate` request, poll `GET /status/{job_id}` — confirm
   it leaves `PENDING` (proves the in-container worker is actually
   consuming the queue, not just that `/health` responds).
6. Restart the Render service manually once (Render dashboard → Manual
   Deploy → Clear build cache & deploy, or just wait for a natural
   redeploy) and confirm Upstash-backed state (an in-flight job, an
   idempotency key) survives — since Upstash, not a Render volume, is what
   persists data here, this is really "does Upstash survive a container
   restart," which it should by construction, but worth confirming once
   rather than assumed, same discipline every phase file in this repo has
   applied to anything not yet run live.
