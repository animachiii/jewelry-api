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
| 4 | Storage & Ingest Pipeline | Real presigned uploads, image validation, asset persistence, retention/expiry lifecycle. | After 2 · **‖ with 3** | No | — | Not started |
| 5 | ~~Matting Worker~~ | **Removed 2026-08-07** — see `docs/decisions/0001-drop-local-matting.md`. Background removal now happens inside Phase 6's Gemini call. | — | No | — | Removed |
| 6 | Gemini Generation Worker | `GenerationProvider` abstraction, pinned model, token-bucket rate limiter, cost logging, refusal handling. Real-photo angles now include background removal in this one call (no matting step). | After 3 | 6a real-photo, 6b synthetic/reference-matrix | — | Not started |
| 7 | Orchestration & Partial Success | Fat payload intake, Celery group fan-out, chord rollup, `PARTIAL_SUCCESS` computation, real `GET /status`. | **Sequential** after 6 | No | — | Not started |
| 8 | Failure Taxonomy & Retry | Failure classification, bounded backoff for transient classes, per-angle retry endpoint, `REJECTED` handling. | Sequential after 7 | No | — | Not started |
| 9 | Output QA Gate | Perceptual similarity scoring for synthetic angles, threshold calibration, human review queue + decision endpoints. | After 6 · **‖ with 7/8** | No | — | Not started |
| 10 | Auth & Security Hardening | Key rotation, per-client rate limits and quotas, secrets management, input sanitization, URL scoping, pen-test pass. | **‖ with 7/8/9** · before 12 | No | — | Not started |
| 11 | Observability & Cost Tracking | Sentry, structlog correlation, Celery/queue metrics, dashboards, alerting, per-job and per-SKU cost reporting. | **‖ from 5 onward** | No | — | Not started |
| 12 | CI/CD & Deployment | Full pipeline, GPU host provisioning, migrations on deploy, staging env, rollback. (Skeleton exists from Phase 0.) | Sequential after 10 | 12a pipeline, 12b GPU infra | — | Not started |
| 13 | Load, Soak & Capacity Tuning | Concurrency ceilings, queue depth under burst, VRAM saturation, Gemini quota behavior at peak, tuned worker counts. | Sequential after 12 | No | — | Not started |
| 14 | V1 Decommission & Cutover | Migrate prompt/reference assets off Higgsfield-era config, parallel-run v1 and v2, ERP cutover, retire n8n bot. | **Sequential** — last | No | — | Not started |

**Recommended order:** 0 → 1 → 2 → (3 ‖ 4) → 6 → 7 → 8 → (9 ‖ 10 ‖ 11) → 12 → 13 → 14

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

---

## Deferred to v3

| Item | Verdict | Note |
| :--- | :--- | :--- |
| Multi-provider abstraction | Defer the provider, **build the seam now** | `GenerationProvider` interface lands in Phase 6. No task body imports the Gemini SDK. |
| Custom local IC-Light relighting | Defer | Revisit only if Phase 9 QA data shows Gemini's native shadow control failing systematically. Let data decide. |
| Queue admin dashboard | Defer | Flower + Grafana in Phase 11 covers ~90% at ~5% of the cost. Build the GUI only on client request. |

---

## Open decisions

Blocking or shaping later phases. Resolve during Phase 0 where possible.

| # | Question | Blocks | Status |
| :--- | :--- | :--- | :--- |
| 1 | ~~Matting model + licensing~~ | — | **Resolved N/A 2026-08-07** — local matting dropped, see decision 0001 |
| 2 | The 7 exact category codes and per-category angle enablement | Phases 3, 6 | Open |
| 3 | GPU host: RunPod / Lambda / bare metal / GCP | Phase 12 | Open |
| 4 | Volume at launch and peak (jobs/day) | Phases 12, 13 | Open |
| 5 | Output image retention policy | Phase 4 | Open |
| 6 | Tenancy — single-client or resold? | Phase 2 schema | Open |
| 7 | Does any n8n v1 generation history need preserving? | Phase 14 scope | Open |
| 8 | Has the client seen and accepted synthetic-angle output quality? | Phase 6b, 9 | Open |

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
