# Capacity Tuning

Phase 13. Every number below is either a config default already in the
repo, or explicitly labeled **estimated** — reasoned from reading the code,
not measured against real traffic. `scripts/load_test.py` (Steps 1-2 of
`phases/phase-13-load-soak-tuning.md`) is what replaces "estimated" with
"measured"; nothing here should be read as a tuned conclusion.

---

## `IO_QUEUE_CONCURRENCY`

Celery `worker`'s prefork pool size — how many sub-jobs one worker process
generates concurrently.

| Deploy path | Current default | Basis |
| :--- | :--- | :--- |
| Fly (`fly.toml`) | `20` | Carried over from the pre-Phase-12 default (`app/config.py`), never load-tested |
| Render free tier (`render.yaml`) | `4` | **Estimated**, conservative guess for a free shared-CPU container, not measured |

Each concurrent generation call holds a DB session, a Redis connection
(rate limiter), and an in-flight HTTP call to Gemini — memory and
connection-count bound, not CPU-bound in steady state. Raising this past
what Step 1's burst test shows as safe risks OOM on Render's free tier
specifically (no configured memory ceiling documented by Render for the
free plan at the time this was written — confirm current numbers before
raising this).

## Gemini rate limiting — global vs. per-client

Two independent limits, and nothing in code enforces a relationship
between them — worth stating explicitly since it's easy to misconfigure:

- **Global** (`app/services/rate_limiter.py`, `GEMINI_RATE_LIMIT_PER_MINUTE`,
  default `60`): one shared bucket across every worker process. This is
  the actual ceiling on Gemini API calls per minute for the whole
  deployment.
- **Per-client** (`app/core/ratelimit.py`, `api_clients.rate_limit_per_min`,
  default `60`, Phase 10): how many `/generate` calls one client can make
  per minute.

**If the global limit is set below the sum of what's useful across active
clients, one busy client can starve every other client's Gemini budget
even while staying under its own per-client limit** — the per-client
check happens at `/generate` time (before dispatch), the global check
happens inside the worker at generation time, and the two never compare
notes. Not a bug to fix in code (rate limiting inherently works this way
without a fair-queuing layer, which isn't built and isn't in scope here) —
a real operational fact worth knowing when setting values, especially
once more than one real client exists (roadmap open decision #6, tenancy,
still open).

## Upstash free-tier command budget

`docs/deployment-free-tier.md` already flags the two hard numbers to
re-check before relying on them: **250MB storage, 500K commands/month**
(Upstash's numbers at the time that doc was written — confirm current
limits, they change).

**Estimated Redis commands per real-photo `/generate` call** (counted by
reading the code paths one call touches, not measured):

| Source | Commands |
| :--- | :--- |
| Idempotency check + store (`app/core/idempotency.py`) | 2 |
| Per-client rate limit incr + expire (`app/core/ratelimit.py`) | 2 |
| Gemini rate limiter incr (`app/services/rate_limiter.py`) | 1 |
| Celery broker publish + result backend writes (`transform_photo` dispatch + result) | ~4 |
| **Total, estimated** | **~10** |

A retry (`/retry`, Phase 8) adds its own `retryidem:` check + store (2
more). A synthetic angle's QA scoring (Phase 9) adds no Redis commands of
its own (no cost tracking, no additional cache reads beyond what
`/generate` already did). `GET /status` polling adds 0 Redis commands per
call in the common case — config is read from the Redis `config:active`
cache only on a cache miss, not every read.

**Back-of-envelope monthly job ceiling on Upstash's free tier**, using the
~10 estimate and assuming most jobs are single-attempt (no retries):

```
500,000 commands/month ÷ 10 commands/job ≈ 50,000 jobs/month
                                         ≈ ~1,650 jobs/day
```

This is almost certainly not the binding constraint at this project's
likely volume (roadmap open decision #4 is still open on exact numbers,
but nothing in this codebase's history suggests anywhere near 1,650
jobs/day) — flagged here so it's a known, checked-off consideration rather
than a silent assumption, not because it's expected to bind in practice.
`scripts/load_test.py --dry-run` prints this same estimate scaled to a
planned test run, specifically so a real load test doesn't itself blow
through the monthly budget by accident.

## VRAM saturation

**N/A.** `docs/decisions/0001-drop-local-matting.md` removed every
GPU/VRAM-bound workload from this codebase. The roadmap's own line for
this phase still names it; nothing to tune because nothing GPU-bound
exists to saturate.

---

## What Steps 1-2 need to actually answer

Run `scripts/load_test.py` against a real deployment (or real
`docker-compose up`, once `GEMINI_API_KEY` is real — every estimate above
that involves a full generation call is meaningless without one, since a
job that fails at the provider call still exercises every Redis command
listed except the ones after that point) and replace every "estimated"
number above with what was actually measured, at minimum:

- p95/p99 latency at increasing `--concurrency`, to find where
  `IO_QUEUE_CONCURRENCY` starts queuing rather than processing immediately
- The concurrency level where `429 RATE_LIMIT_EXCEEDED` /
  `429 QUOTA_EXCEEDED` start appearing, confirming the configured limits
  actually trigger where expected
- A multi-hour soak run's health-check time series, to catch degradation a
  burst test can't show
