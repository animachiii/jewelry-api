# 0002 — Background removal and replacement both go through Gemini; no alpha channel

**Date:** 2026-08-12
**Status:** Accepted

## Context

`phases/phase-15-background-operations.md` Step 1 required a timeboxed spike
over ≥12 real client pieces (≥3 fine chains, ≥2 transparent/translucent
stones, ≥2 high-polish metal surfaces) comparing four options — (A) Gemini
with a flat/solid background, (B) a hosted matting API, (C) Vertex AI, (D)
local BiRefNet — before any Phase 15 code was written, because only a real
alpha channel makes `BACKGROUND_REMOVAL` composable onto arbitrary
backgrounds, and the Gemini API's `ImageConfig.output_mime_type`/
`.image_output_options` are documented "not supported in Gemini API"
(Vertex-only), with every production output so far being `image/jpeg`.

**That spike did not run.** This environment has no real `GEMINI_API_KEY`
(empty in `.env`, same gap every prior phase note already recorded), no
Vertex-capable service account (`GOOGLE_SERVICE_ACCOUNT_JSON` is an unset
placeholder), no credentials for any hosted matting API, and — unlike every
prior phase, which at least had seeded/fixture image bytes to work with —
**zero image files anywhere in the repo**, real or fixture. There was no way
to produce the evidence Checkpoint 1 asks for. This was surfaced to the user
directly rather than fabricated.

## Decision

The user decided directly, without the spike: **both `BACKGROUND_REMOVAL`
and `BACKGROUND_REPLACEMENT` go through Gemini** — the existing
`GenerationProvider` / `GeminiProvider` seam, no new provider, no new
infrastructure. This is Option A from the phase file, extended to cover
replacement too (replacement was already going to be Gemini-based per Step
3's preset-into-prompt design; the new information here is that removal
joins it rather than getting a hosted matting API per Option B).

**Consequence the user has accepted:** output has no alpha channel. A
"removed background" is a flat/solid background, the same shape a
replacement job produces, not a transparent cutout. `BACKGROUND_REMOVAL`
in practice means "put the product on a neutral/standard backdrop," not
"give me a PNG I can composite anywhere." If a genuine transparent cutout
is needed later, revisit this decision — the fallback is still Option B
(hosted matting API, e.g. Bria's hosted RMBG which sidesteps the CC BY-NC
licensing problem that blocked the self-hosted model) or Option C (Vertex,
*if* it's verified to actually return real varying alpha — never assumed).
Option D (local BiRefNet) stays ruled out; nothing in this decision changes
the GPU/512MB-instance constraint.

**Where Gemini's key actually lives:** the key exists in the Render
production environment, not in this local `.env` — consistent with every
prior phase's "no real key in this dev environment" note. Phase 15's code
is being built and unit/integration-tested the same way Phases 6/9/13 were:
fixture-driven locally, real verification deferred to the live instance.

## Checkpoint 1 — honest status, not a pass

- [x] Decision made and written up — but **without** the spike's evidence
      requirement. This is a deliberate, user-directed shortcut, not a
      silent skip.
- [ ] Spike run over ≥12 real pieces — **not done**, no real photos or local
      key existed to run it
- [ ] Per-candidate alpha verification (PNG colour type 4/6, non-uniform
      alpha) — **not applicable**, only one candidate (Gemini) was evaluated
      and it is already known from `docs/ai-integration.md` to return
      `image/jpeg`
- [ ] Per-image latency/cost recorded — **not done** locally; Step 5's
      checkpoint still requires measuring real end-to-end latency against
      the live Render instance, which is where this will actually get
      verified
- [x] No new dependency, no new credentials table entry needed — Option A
      requires nothing `docs/deployment.md` doesn't already have
- [x] Client (the user, directly in chat, 2026-08-12) confirmed a flat/solid
      background meets the need for **both** operations before any Step 2+
      code was written

## Consequences

- `operations.BACKGROUND_REMOVAL` and `operations.BACKGROUND_REPLACEMENT`
  in the config payload (Step 3) both resolve to a Gemini prompt, same
  provider call shape as Mode A angle generation — one image in, one image
  out, `image/jpeg`.
- No new provider module, no new `Settings` fields, no new secrets-table
  entry.
- The subject-preservation QA gate (Step 5) still applies to both — Gemini
  can still drift the product even without the transparency question, and
  that's the risk this gate exists to catch.
- If the client later asks for real transparency, that reopens this
  decision as a new one (0003+), not a silent scope change here.
