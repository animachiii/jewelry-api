# Phase 13 — Load, Soak & Capacity Tuning

## Reality check before writing this

**This phase cannot be completed this session, and the phase file says so
plainly rather than papering over it.** Every prior phase — even Phase 12,
which is the closest precedent — could produce something checkable: config
that parses, a container that actually runs locally against real Supabase
credentials. Load and soak testing is different in kind: its entire purpose
is measuring behavior *under real concurrent traffic against a real
deployment*, and no deployment exists yet (Phase 12's own closing note
already said as much: "Phase 13 can't start for real until the user has
actually completed the manual steps this phase's checkpoints leave open").
Confirmed with the user directly rather than assumed: build the tooling and
a reasoned starting-point tuning doc now, run it for real later.

**What "VRAM saturation" (the roadmap's own wording) means here: nothing.**
`docs/decisions/0001-drop-local-matting.md` already removed every
VRAM-bound workload from this codebase — there is no GPU anywhere in this
stack. Marked N/A below, not silently dropped.

**What's real and load-bearing for this phase's actual scope:**

- `app/config.py::IO_QUEUE_CONCURRENCY` — Celery `worker`'s prefork pool
  size (`app/workers/celery_app.py`, `render_start.sh`, `fly.toml`). Set to
  `20` by default, `4` in `render.yaml`'s free-tier env — neither value has
  ever been tested against real concurrent load.
- `app/services/rate_limiter.py` — the *global* Gemini token bucket
  (`settings.GEMINI_RATE_LIMIT_PER_MINUTE`, default `60`), shared across
  every worker process. Real and tested against fakeredis (Phase 6), never
  against real burst traffic.
- `app/core/ratelimit.py` — *per-client* rate limiting (Phase 10),
  `rate_limit_per_min` on `api_clients`, default `60`.
- **Upstash's free-tier command cap** (`docs/deployment-free-tier.md`
  already flags this: "check current numbers before relying on this... V1's
  own docs note 250MB storage / 500K commands per month") is the tightest
  real constraint on the free-tier deploy path specifically — a burst load
  test against the free-tier deployment could itself exhaust the month's
  command budget, which the load-test tooling needs to guard against, not
  just measure past.

---

## Step 1 — Load-test tooling (burst)

### What to do

`scripts/load_test.py` — a standalone script using `httpx` (already a
project dependency, same client class every integration test already uses,
no new load-testing framework/DSL to learn or install) and `asyncio` for
concurrency, not a new tool like k6/Locust. CLI args: `--base-url`,
`--api-key`, `--concurrency`, `--requests`, `--category` (defaults `RING`),
`--angle` (defaults `FRONT`, real-photo — synthetic angles hit the QA
provider too, a different cost profile worth testing separately, not
defaulted into the basic run).

For each concurrent "virtual client": presign an upload, PUT a real tiny
JPEG, `POST /generate`, then poll `GET /status/{job_id}` until terminal or
a timeout. Records per-request latency, HTTP status, and specifically
counts `429`s by `error.code` (`RATE_LIMIT_EXCEEDED` vs `QUOTA_EXCEEDED` vs
Upstash exhaustion surfacing as a `503`/connection error) — the three have
different tuning implications and the script must not conflate them.
Prints p50/p95/p99 latency and a full status/error-code breakdown at the
end. A `--dry-run` flag prints the request plan without sending anything —
lets the user sanity-check concurrency×requests against Upstash's monthly
command budget before spending it.

### Checkpoint 1

- [x] Script runs against **local** `docker-compose up` (real local
      Postgres/Redis, fixture-free — needs `MOCK_MODE`-independent real
      `/generate`, already true since Phase 2) at low concurrency, proving
      the script itself is correct — this is checkable now, without a live
      deployment
- [ ] **User-only:** a real run against the live Render/Fly deployment,
      at increasing concurrency, to find where p95 latency or error rate
      inflects — the actual point of this phase, needs the deployment to
      exist first

---

## Step 2 — Soak-test mode

### What to do

Same script, `--soak-duration-minutes` flag instead of `--requests`: holds
a low, sustained request rate (default 1 new job per virtual client per 30
seconds — a starting guess for "occasional real traffic," not documented
anywhere as the ERP's actual submission cadence, which isn't specified in
`docs/integration-guide.md`; only the `poll_after_ms`/`Retry-After` *status
polling* cadence is documented there, a different thing from how often new
jobs get submitted) for the given duration instead of a short burst, and
additionally samples `GET /api/v2/health` every minute to catch gradual
degradation (a growing DB connection count, Redis memory pressure) that a
short burst wouldn't surface. Prints a time-series summary, not just
a final aggregate, so a slow drift is visible rather than averaged away.

### Checkpoint 2

- [x] Soak mode runs locally for a short duration (2-3 minutes) without
      crashing or leaking connections — checkable now
- [ ] **User-only:** a real multi-hour soak against the live deployment —
      the free-tier path's cold-start-on-idle behavior
      (`docs/deployment-free-tier.md`) makes "sustained low-rate traffic"
      itself an interesting real test: does it keep the Render service
      awake, or does spacing requests 30s apart still let it sleep between
      them? Only answerable by actually running it

---

## Step 3 — Tuning guidance (reasoned starting point, not measured)

### What to do

`docs/capacity-tuning.md` (new): back-of-envelope reasoning for starting
values, explicitly framed as a hypothesis for Step 1/2 to correct, not a
conclusion:

- `IO_QUEUE_CONCURRENCY`: Render free tier's container has limited
  CPU/memory (`docs/deployment-free-tier.md` doesn't pin an exact spec —
  Render's free plan is a shared, small instance); `render.yaml`'s `"4"`
  is a conservative guess pending Step 1 data, not a measured value. Fly's
  `512mb` worker VM (`fly.toml`) similarly untested at its configured `20`.
- `GEMINI_RATE_LIMIT_PER_MINUTE` (global) vs. per-client `rate_limit_per_min`:
  the global bucket must be ≥ the sum of what's useful across all clients,
  or a single busy client can starve every other client's budget even
  though it's under its own per-client limit — worth stating explicitly
  since nothing enforces this relationship in code today.
- Upstash free-tier command budget: every Celery task dispatch, every
  `idem:`/`retryidem:`/`ratelimit:` key touch, every Gemini rate-limit
  bucket increment is a Redis command. A rough per-job command count
  (counted by reading the code paths a single real-photo `/generate` call
  touches, not measured against a live Upstash instance) gives a rough
  jobs/month ceiling before the free tier's cap binds — the actual
  multiplier needs Step 1's real run to confirm, this section only sets up
  the arithmetic.

### Checkpoint 3

- [x] `docs/capacity-tuning.md` exists and every number in it traces to
      either a config default already in the repo or an explicit
      "estimated, not measured" label — no number presented as tested
      when it isn't
- [ ] **User-only:** replacing every "estimated" number with a real one
      from Steps 1-2's actual runs

---

## Step 4 — Self-audit

Same discipline as every prior phase, adapted to what's actually
checkable this session: `scripts/load_test.py` runs correctly against a
real local stack (`docker-compose up`, not testcontainers — this script is
meant to be run by a human against a running server, not invoked from
pytest). Sync `phases/phase-roadmap.md` and `CLAUDE.md` to state plainly
that tuning numbers are estimated pending a real deployment, not to claim
tuning happened. Add a roadmap open decision if `docs/capacity-tuning.md`'s
Upstash command-budget arithmetic surfaces something that should block
going live on the free tier at expected volume (ties into open decision
#4, still unresolved on exact volume).

---

## Note for Phase 14

Phase 14 (V1 Decommission & Cutover) is the last phase and doesn't depend
on this one structurally, but a parallel-run cutover (mentioned in its own
roadmap line) implies both v1 and v2 handling real traffic simultaneously
for some period — exactly the kind of load this phase's tooling exists to
characterize beforehand, not discover for the first time during a cutover.
