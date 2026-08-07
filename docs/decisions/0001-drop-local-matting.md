# 0001 — Drop local matting; Gemini handles background removal directly

**Date:** 2026-08-07
**Status:** Accepted

## Context

The original v2 design (see `docs/ai-integration.md` Call Site 1, pre-2026-08-07)
ran every real-photo angle through a local BiRefNet-matting model on a
GPU-backed Celery queue before sending the photo + alpha matte to Gemini.
Phase 0 Step 5 required benchmarking BiRefNet against RMBG-2.0 on ≥30 real
client pieces and provisioning GPU access to do it.

That benchmark was never run. Evaluating the cost of GPU infrastructure
(rental or owned, plus the ops overhead of a VRAM-bound queue,
`worker_process_init` model loading, and licensing diligence) against a
single Gemini call that already does relighting and background synthesis,
the GPU step stopped looking worth it for this project's volume.

## Decision

Real-photo angles (Mode A) now send the source photograph directly to
Gemini with the category/angle prompt — no separate matting step, no local
model, no `gpu` Celery queue. This is the same shape Mode B (synthetic
angles) already used. `MATTING` as a `sub_job_status_t` value and `MATTE`
as an `asset_kind_t` value are **not removed from the schema** — Postgres
enum value removal requires recreating the type, which isn't worth the risk
for values no code path produces anymore. They're vestigial; new code must
not set them.

## Consequences

- No GPU hosting, no `worker_process_init` model-loading concern, no
  matting-model licensing question (Phase 0 Step 5 is moot — the open
  decision in `phases/phase-roadmap.md` is marked resolved as N/A).
- Celery collapses to a single `io` queue — the `gpu`/`io` split existed
  only to isolate VRAM-bound work, and there is none left.
- **Quality risk, accepted deliberately:** `docs/ai-integration.md`
  previously flagged that matte-anchoring was specifically what kept real
  photos "high-fidelity" — jewelry's hard cases (fine chain, transparency,
  specular highlight) are exactly where an unanchored generative edit is
  more likely to drift from the source. There is no QA gate on Mode A
  results the way there is on synthetic (Mode B) angles. If real-world
  output quality on these hard cases turns out to be unacceptable, revisit
  this decision — the fallback is either reintroducing local matting or
  routing Mode A through the same QA similarity gate Mode B uses.
- `jewelry-mattes` Supabase bucket deleted; `BUCKET_MATTES`,
  `MATTING_MODEL_ID`, `MATTING_MODEL_REVISION`, `GPU_QUEUE_CONCURRENCY`
  removed from settings and `.env.example`.
