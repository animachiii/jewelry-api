# Phase Roadmap — AI Jewelry Generation API (v2)

**Living index. Update `Status` manually as work progresses.**
Check this file before generating or starting any phase.

Status values: `Not started` · `In progress` · `Complete`

---

## Phases

| # | Phase | Description | Dependency | Split? | File | Status |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 0 | Foundation & Environment | Repo scaffold, Supabase Postgres + full schema, Storage buckets, Redis/Celery, seed data, CI skeleton. Matting benchmark dropped — see decision 0001. | **Sequential** — blocks all | No | `phase-0-foundation.md` | **Complete** — fully verified live against real Supabase (schema, storage, seed data), CI green with branch protection |
| 1 | API Contract & Mock Server | Full OpenAPI 3.1 spec, real auth, mock fixtures for all 8 job states. **Client + Flutter sign-off gate.** | Sequential after 0a | No | `phase-1-api-contract.md` | **Built & verified, sign-off pending** — spec/auth/fixtures done against real Supabase; no deployment for the Flutter team yet and the actual sign-off walkthrough hasn't happened (needs a human session) |
| 2 | Data Model & Job State Machine | Repositories, job/sub-job creation, state transitions, parent-status rollup, idempotency, `job_events` audit trail. | Sequential after 1 | No | `phase-2-data-model.md` | **Complete** — verified live against real Supabase; `/generate` is real, `/retry` still MOCK_MODE (Phase 8) |
| 3 | Config Service | Sheets → versioned Postgres snapshot → Redis cache. Real `GET /config`. Cold-cache and Sheets-outage fallback. Beat sync task. | After 2 · **‖ with 4** | No | `phase-3-config-service.md` | **Complete** — verified against real local Redis + testcontainers Postgres; Sheets sync exercised for real via its actual outage path (no Sheets project exists yet, per open decision #2), not mocked |
| 4 | Storage & Ingest Pipeline | Real presigned uploads, image validation, asset persistence, retention/expiry lifecycle. | After 2 · **‖ with 3** | No | `phase-4-storage-ingest.md` | **Complete** — verified live against real Supabase Storage; structural image validation, real asset metadata, `purged_at`-tracked retention worker |
| 5 | ~~Matting Worker~~ | **Removed 2026-08-07** — see `docs/decisions/0001-drop-local-matting.md`. Background removal now happens inside Phase 6's Gemini call. | — | No | — | Removed |
| 6 | Gemini Generation Worker | `GenerationProvider` abstraction, pinned model, token-bucket rate limiter, cost logging, refusal handling. Real-photo angles now include background removal in this one call (no matting step). | After 3 | 6a real-photo, 6b synthetic/reference-matrix | `phase-6-generation-worker.md` | **Complete** — verified against testcontainers Postgres + real local Redis + real Supabase Storage; only the Gemini call itself is fixture-driven (no real API key exists). Nothing calls the task yet — Phase 7 wires it to real jobs |
| 7 | Orchestration & Partial Success | Fat payload intake, Celery group fan-out, chord rollup, `PARTIAL_SUCCESS` computation, real `GET /status`. | **Sequential** after 6 | No | `phase-7-orchestration.md` | **Complete** — fan-out implemented as independent dispatches + per-transition recompute, not a literal group/chord (see phase file); a real job now runs to a terminal status end-to-end |
| 8 | Failure Taxonomy & Retry | Failure classification, bounded backoff, `REJECTED` handling were already built (Phase 6/7); this phase's real scope was making the per-angle retry endpoint real. | Sequential after 7 | No | `phase-8-failure-retry.md` | **Complete** — `POST /retry` real, `retryidem:` idempotency, `retryable` ceiling fix, verified against testcontainers Postgres + real local Redis + real Supabase Storage, fixture-driven Gemini |
| 9 | Output QA Gate | LLM-judged similarity scoring for synthetic angles (threshold calibration not achievable without real client pieces — see open decision #8), human review queue + decision endpoints. | After 6 · **‖ with 7/8** | No | `phase-9-qa-gate.md` | **Complete** — `QaProvider`/`GeminiQaProvider` abstraction, real scoring wired into the pipeline (fail-open-to-human on provider error), real `/qa/review-queue` + `/qa/{sub_job_id}/decision`, verified against testcontainers Postgres + real local Redis + real Supabase Storage, fixture-driven Gemini |
| 10 | Auth & Security Hardening | Real per-client rate limits + daily quotas on `/generate` (built but dead code since Phase 0/1), secret-logging static audit, URL-scoping regression test. Key rotation and pen-test explicitly deferred — see phase file. | **‖ with 7/8/9** · before 12 | No | `phase-10-auth-security.md` | **Complete** — `RateLimitError`/`QuotaExceededError` wired into `/generate` with real `Retry-After`, static secret-leakage audit test, URL-scoping regression test; key rotation has no route in `docs/api-routes.md` (open decision #9), pen-test needs a live deployment (Phase 12) |
| 11 | Observability & Cost Tracking | Sentry, structlog correlation, Celery/queue metrics, dashboards, alerting, per-job and per-SKU cost reporting. | **‖ from 5 onward** | No | — | Not started |
| 12 | CI/CD & Deployment | Real CD, migration-on-deploy, staging env, documented rollback. GPU host provisioning is moot — see decision 0001. (CI skeleton existed from Phase 0.) Two deploy paths: Fly.io+Upstash (card required, config-only) and Render+Upstash (cardless, verified locally end-to-end — see addendum in phase file). | Sequential after 10 | No | `phase-12-cicd-deployment.md` | **Render path locally verified live; Fly path config-only** — `docker build` + real container run confirmed `render_start.sh` runs migrations then all three processes (uvicorn/celery beat/celery worker) and `GET /api/v2/health` returns 200 against real Supabase + local Redis. `fly.toml`/`fly.staging.toml` still valid but never account-tested (Fly requires a card). Neither path has a real cloud account behind it yet — that's the user's remaining step in either `docs/deployment.md` or `docs/deployment-free-tier.md` |
| 13 | Load, Soak & Capacity Tuning | Concurrency ceilings, queue depth under burst, Gemini quota behavior at peak, tuned worker counts. VRAM saturation is moot — see decision 0001. | Sequential after 12 | No | `phase-13-load-soak-tuning.md` | **Tooling built and verified against a real local server; live-deployment numbers not yet measured** — `scripts/load_test.py` (burst + soak modes) actually run against a real local API+worker+Supabase, correctly recorded a real bug it found (see below) as a TIMEOUT rather than crashing. `docs/capacity-tuning.md`'s numbers are explicitly labeled estimated pending a real deployment to measure against |
| 14 | V1 Decommission & Cutover | Migrate prompt/reference assets off Higgsfield-era config, parallel-run v1 and v2, ERP cutover, retire n8n bot. | **Sequential** — last | No | — | Not started |
| 15 | Standalone Background Operations | `BACKGROUND_REMOVAL` + `BACKGROUND_REPLACEMENT` as one-image-in/one-image-out operations, independent of the four-angle flow. Adds `jobs.operation`, nullable `sub_jobs.angle`, curated backdrop presets, and a subject-preservation QA gate. Reuses the existing state machine, status, retry, and cost paths. | After 13 · **‖ with 11/14** | No — Step 1 landed without a new provider | `phase-15-background-operations.md` | **Complete** — verified against testcontainers Postgres + real local Redis + real Supabase Storage, fixture-driven Gemini (no real `GEMINI_API_KEY` exists in this environment, same gap every prior phase hit). Step 1 decided directly by the user rather than via the planned 12-piece spike; both operations go through Gemini, flat/solid background, no alpha channel — `docs/decisions/0002-background-removal-approach.md`. Steps 2-6 all built and tested: schema (`operation_t`, nullable `sub_jobs.angle`/`jobs.category_code`, `jobs.preset_code`), config (presets + per-operation cost/prompt + its own QA threshold), both routes + job-level retry, real worker + QA gate. Two real bugs found and fixed (parent status never reaching `PROCESSING`; `GET /qa/review-queue` crashing on a background item) plus a pre-existing validation-error-serialization bug unrelated to this phase. Not done: live-instance latency (no reachable deployment) and the Flutter-lead written sign-off (needs a human session) — see CLAUDE.md's Phase 15 entry |
| 16 | Stability Closeout | Celery task time limits, a reconciliation sweep for stuck sub-jobs, one-time cleanup of 23 pre-existing orphaned sub-jobs (found live: 23 `ANGLE_GENERATION`/`BACKGROUND_REMOVAL` sub-jobs, not the 15 first estimated — count grew between the diagnostic session and this phase's execution), RLS-is-intentional verification, storage anomaly audit, `OUTPUT` retention value. Not in the original 15-phase plan — added after a live Render/Supabase diagnostic session on 2026-08-15 found the free-tier OOM cycle already fixed in code (2026-08-13) but its symptoms never cleaned up, and no backstop for a future hang. | Sequential — before 17 | No | `phase-16-stability-closeout.md` | **Complete** — verified live against real Render + real Supabase. Task timeout enforcement had to move from Celery's `task_time_limit`/`task_soft_time_limit` (confirmed inert under this deployment's `--pool=solo`) to an `asyncio.wait_for` wrapper instead — found while implementing, not anticipated by the phase file. Reconciliation sweep scoped to PENDING/GENERATING only, deliberately excluding QA_REVIEW (a legitimate human-review wait, not a stuck state — the phase file's original "any non-terminal status" sketch would have wrongly swept it). Storage anomaly root-caused to the test suite uploading real bytes to real Storage with no cleanup (fixed via a new autouse pytest fixture), not a production bug; 57,022 orphaned test objects (176MB) deleted live. A related latent bug in `app/workers/retention.py` (same shared-engine + bare-`asyncio.run()` shape already documented as crashing `config.py` in production) was found and fixed while building the new reconciliation worker on the same pattern. |
| 17 | AWS Deployment (App Runner) | Client-mandated move off Render's free tier. ECR + App Runner, replacing Render as primary while keeping it and Fly as fallback paths, per `docs/decisions/0003-deploy-to-aws.md`. | Sequential after 16 | No | `phase-17-aws-deployment.md` | **Pipeline/config complete, no AWS account behind it yet** — same category of "done" as Phase 12's Fly path. `.github/workflows/deploy-aws.yml` is valid, OIDC-authenticated (not a static key, unlike the Fly path), gated on CI via `workflow_run`. `docker build .` against the unmodified `Dockerfile` succeeds and the image's `CMD` is still `./scripts/render_start.sh`, confirmed live in this session. `docs/deployment-aws.md`'s secrets table is cross-checked field-by-field against `app/config.py::Settings`. Render (`docs/deployment-free-tier.md`) stays **primary** until App Runner is verified live — that verification needs a real AWS account, ECR repositories, an OIDC IAM role, and an App Runner service, none of which this session can create; see that doc's "What only the user can do" section for the exact remaining steps. **Blocked as of 2026-08-15 — user reports AWS account access stuck on a missing prerequisite; not something this session can diagnose without knowing what AWS returned. v3 feature phases (18+) do not depend on this and proceed in parallel.** |
| 18 | MATCH (Companion-Piece Generation) | First v3 feature. New operation type reusing the job/sub-job/asset state machine, styled after Phase 15's shape — a source piece as a style reference, N generated companion-piece variants (1-4), no mask, no compositing. Requires a schema fix to `ux_sub_jobs_job_single` (Phase 15's angle-less-implies-single-sub-job assumption breaks for multi-variant MATCH jobs). | **Independent of 17 (AWS)** · Sequential after 16 | No | `phase-18-match.md` | Not started |

**Recommended order:** 0 → 1 → 2 → (3 ‖ 4) → 6 → 7 → 8 → (9 ‖ 10 ‖ 11) → 12 → 13 → 14 → 16 → (17 ‖ 18)

---

## Where the cross-cutting concerns live

Called out explicitly so none of these defaults to "polish at the end."

| Concern | Where | Note |
| :--- | :--- | :--- |
| **Automated testing** | Phase 0 (harness) + every phase's checkpoints + Phase 13 (load/soak) | Deliberately not a single late phase — a testing phase at the end is the phase that gets cut. |
| **Auth & security** | Phase 10 dedicated; auth implemented for real in Phase 1; secrets in Phase 0 | Parallel-eligible so it is never schedule-blocked. Tenancy decided in Phase 2 (schema question). |
| **Deployment & CI** | CI skeleton Phase 0; full deploy Phase 12 | CI on day one so it never has to catch up on accumulated debt. |
| **Data migration** | Phase 14 | Light — prompts already live in Sheets. Real work is asset migration + ERP cutover. **Grows if the client needs n8n generation history preserved.** |
| **Monitoring & error tracking** | Phase 11 dedicated | Cost tracking bundled here; shares the same instrumentation path. |
| **Production incident response** | Phase 16, ad hoc thereafter | Not one of the original five cross-cutting concerns — added because this project is now live with real client traffic, which the original 15-phase plan (built before any live deployment existed) didn't need to account for |

---

## Deferred to v3

| Item | Verdict | Note |
| :--- | :--- | :--- |
| Multi-provider abstraction | Defer the provider, **build the seam now** | `GenerationProvider` interface lands in Phase 6. No task body imports the Gemini SDK. |
| Custom local IC-Light relighting | Defer | Revisit only if Phase 9 QA data shows Gemini's native shadow control failing systematically. Let data decide. |
| Queue admin dashboard | Defer | Flower + Grafana in Phase 11 covers ~90% at ~5% of the cost. Build the GUI only on client request. |

**v3 feature phases** (companion-piece generation, masked recolor, masked mix) start at Phase 18. Only Phase 18 is written in detail — per this file's own rule 1, RECOLOR and MIX are not generated until 18 is actually built and verified.

---

## Open decisions

Blocking or shaping later phases. Resolve during Phase 0 where possible.

| # | Question | Blocks | Status |
| :--- | :--- | :--- | :--- |
| 1 | ~~Matting model + licensing~~ | — | **Resolved N/A 2026-08-07** — local matting dropped, see decision 0001 |
| 2 | The 7 exact category codes and per-category angle enablement | Phases 3, 6 | Open |
| 3 | ~~GPU host: RunPod / Lambda / bare metal / GCP~~ | — | **Resolved N/A 2026-08-07** — moot, see decision 0001; the real Phase 12 question ("where does the stateless app deploy") was decided directly with the user: **Fly.io** (API + worker + beat) + **Upstash** (Redis) |
| 4 | Volume at launch and peak (jobs/day) | Phases 12, 13 | Open — `fly.toml`'s machine sizes (`shared-cpu-1x`, 256-512mb) are a starting guess for near-zero real traffic, not sized against real volume; `docs/capacity-tuning.md`'s ~50,000 jobs/month Upstash-command estimate is the other number waiting on this |
| 5 | Output image retention policy | Phase 4, closed by Phase 16 | **Defaulted, not resolved** — Phase 16 sets `RETENTION_DAYS['OUTPUT'] = 180` days absent a client answer, driven by real storage-capacity pressure (484MB of Supabase's 500MB free tier, found 2026-08-15, see `docs/storage-audit-2026-08.md`). The client can change this to any value at any time; it was not left `None` indefinitely because the free-tier ceiling made that a real, near-term risk rather than a theoretical one |
| 6 | Tenancy — single-client or resold? | Phase 2 schema | Open |
| 7 | Does any n8n v1 generation history need preserving? | Phase 14 scope | Open |
| 8 | Has the client seen and accepted synthetic-angle output quality? | Phase 6b, 9 | Open — blocks `qa_similarity_threshold` calibration (Phase 9 built the mechanism, `0.82` is still a placeholder). Phase 15 added a second, independently-tunable threshold in the same uncalibrated state: `background_qa_similarity_threshold` (`0.92`, migration 0010) |
| 9 | Key rotation — who can rotate whose key (self-service `client` scope vs. `ops`-only), grace period for the old key, in-flight-request handling | Phase 10 | Open — found during Phase 10; no route for this exists anywhere in `docs/api-routes.md`, so building one would mean inventing undocumented API surface rather than implementing a spec |
| 10 | Background removal — is a **flat solid background acceptable**, or does the client need genuine transparency (alpha) for compositing? | Phase 15 | **Resolved 2026-08-12** — flat/solid background accepted, decided directly by the user without the planned 12-piece spike (no real Gemini key or client photos existed locally to run one). Both `BACKGROUND_REMOVAL` and `BACKGROUND_REPLACEMENT` go through Gemini, no alpha channel. See `docs/decisions/0002-background-removal-approach.md` |
| 11 | Which backdrop presets does the client actually want for background replacement? | Phase 15 | Open — same conversation as the angle prompt matrix. The real Sheet has no Global tab, so presets cannot be authored there without a Sheet layout change; they have to be seeded by migration and inherited forward |
| 12 | Real target-category prompt wording for MATCH, and real per-variant pricing | Phase 18 | Open — `docs/decisions`-style placeholder seeded in migration `0014`, same uncalibrated status as every other seeded prompt/cost in this project |

---

## Rules for generating the next phase file

1. **Never generate more than one phase ahead of current progress.** A phase file written
   three phases early describes a codebase that will not exist.
2. Before generating phase N, state what is *actually true* about the codebase — including
   anything that diverged from this roadmap. The phase file is written against reality.
3. If a phase's checkpoints exceed ~8–10 items or span unrelated concerns, split it into
   `phase-Na-*.md` and `phase-Nb-*.md` rather than cramming.
4. Every checkpoint must be specific and testable. "Page loads correctly" is not a
   checkpoint. "A `client`-scope key requesting another client's `job_id` returns 404, not
   403" is.
5. A phase is not complete until `docs/` matches what was built. Documentation drift is a
   bug and the self-audit checks for it.
