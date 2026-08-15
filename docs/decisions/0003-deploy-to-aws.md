# 0003 — Deploy to AWS App Runner, replacing the Render free-tier path

**Date:** 2026-08-15
**Status:** Proposed — target platform recommended below, final account setup is the user's step (see Phase 17)

## Context

`docs/decisions/` 0001 and 0002 already established that this codebase is deliberately unexciting from an infrastructure standpoint: no GPU, no VRAM-bound work, one stateless container image running API + Celery worker + Celery beat together. Phase 12 chose Fly.io + Upstash as the intended target but that path was never actually deployed — no Fly account was ever created. What's actually live in production is the fallback path, `docs/deployment-free-tier.md`'s Render free tier, which is what the client has been testing against.

That Render free tier has been crashing. The client, directly, has mandated moving to AWS as a result. This is not being revisited or second-guessed here — it's the client's infrastructure decision to make, for reasons (billing relationship, existing AWS footprint, internal policy) this project doesn't need to know.

**What's worth stating plainly, though, because it changes what Phase 17 should and shouldn't be:** the Render crashes were a real, diagnosable, code-level memory problem — a forked Celery child holding `google-genai` alongside everything else didn't fit a 512MB container — not a platform limitation AWS inherently solves. `phases/phase-16-stability-closeout.md` fixes the remaining gaps around that same class of problem (task time limits, a reconciliation sweep) before this move happens, specifically so AWS isn't asked to run the same unfixed system on a bigger, more expensive box. Moving to AWS is still worth doing — a client-owned cloud account is a reasonable ask on its own terms, independent of the crash history — but it isn't a substitute for Phase 16.

## Decision

**AWS App Runner**, not ECS Fargate, not EC2.

Reasoning:

- This project's deployable unit has been one container running three processes together (`scripts/render_start.sh`'s pattern) since the Render fallback was built. App Runner runs exactly that shape — point it at a container image, it runs it, no VPC, no load balancer, no task definitions, no service mesh to stand up. It is the closest AWS analog to what Render already does, which means the container, `Dockerfile`, and `render_start.sh` script need **no changes** to move.
- ECS Fargate would let API and worker scale independently, which is a real advantage this project doesn't currently need at 20–30 generations/day, and which would require splitting the single container into two task definitions, a shared VPC, service discovery or a load balancer for the API, and meaningfully more ongoing AWS surface to operate. That's the right answer if volume grows enough to need independent scaling — not the right answer for a first AWS deployment of a system that just needs to stop crashing.
- EC2 directly is strictly more operational surface than App Runner for identical capability — patching, scaling, and health-check wiring App Runner already does. Nothing about this project's workload (still no GPU, still no VRAM, confirmed by decision 0001) argues for owning a raw instance.

Supabase Postgres and Upstash Redis are **unchanged** — both are reachable over the public internet with TLS, exactly as they are from Render today, and neither needs to move for this decision to be satisfied. `DATABASE_URL` and the `REDIS_URL`/`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND` secrets carry forward as-is.

## Consequences

- The Render free-tier path (`render.yaml`, `docs/deployment-free-tier.md`) is retired once App Runner is verified live, not deleted immediately — kept as a fallback the same way `fly.toml` was kept after the Render pivot, per this repo's own precedent of not deleting a working deploy path the moment a new one is chosen.
- `fly.toml`/`fly.staging.toml` and the Fly-specific half of `.github/workflows/deploy.yml` remain valid and untouched. Nothing about this decision closes the door on Fly if App Runner or the AWS relationship changes later.
- Container images move from "built inline by Render's dashboard integration" to "built by CI, pushed to Amazon ECR, deployed by App Runner" — a real new pipeline, detailed in `phases/phase-17-aws-deployment.md`.
- Secrets move from Render's dashboard Environment tab to AWS Secrets Manager, referenced by the App Runner service configuration rather than set as plain environment variables — a small but real improvement over both prior paths, neither of which used a secrets manager.
- If the client's actual requirement turns out to be "ECS Fargate specifically" or "EC2 specifically" rather than "AWS, whichever service fits" — that hasn't been confirmed either way — this decision is easy to revisit: nothing built in Phase 16 or the container image itself is App-Runner-specific.
