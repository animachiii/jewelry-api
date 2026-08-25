# AI Integration

Every place a model runs. Two call sites, one queue, one abstraction boundary.

**2026-08-07:** local matte extraction (BiRefNet/RMBG-2.0) was dropped —
see `docs/decisions/0001-drop-local-matting.md`. Real-photo angles now go
straight to Gemini, same shape as synthetic angles. There is no `gpu`
queue anymore.

**2026-08-12 (Phase 15):** background operations (`BACKGROUND_REMOVAL` /
`BACKGROUND_REPLACEMENT`) reuse both call sites below exactly as they
stand — same provider abstractions, same queue, no new provider code — via
a separate worker/service module (`app/workers/background.py`,
`app/services/background_service.py`). See Mode C under Call site 1, the
background note under Call site 2, and
`docs/decisions/0002-background-removal-approach.md` for why no
alpha-channel path exists.

**2026-08-16 (Phase 18):** `MATCH` (companion-piece generation) is a third
reuse of Call site 1, via its own `app/workers/match.py` /
`app/services/match_service.py`. See Mode D under Call site 1. Unlike Modes
A-C, its source image is a style reference rather than the subject being
transformed, and it has no QA gate at all (not even Mode C's unconditional
one) — see Mode D for why.

**2026-08-16 (Phase 19):** `RECOLOR` (masked gemstone recolor) is a fourth
reuse of Call site 1, via `app/workers/recolor.py` /
`app/services/recolor_service.py`. See Mode E under Call site 1. The first
mode where the client-facing output is not the provider's raw response —
Gemini has no mask parameter, so RECOLOR conveys the mask as a colour
overlay before the call and uses it to composite the result back onto the
original source afterward.

---

## Call site 1 — Image generation

| | |
| :--- | :--- |
| **Trigger** | Sub-job enters `GENERATING` |
| **Where** | Celery `io` queue, `app/workers/generation.py` via `app/providers/gemini.py` |
| **Model** | Gemini Image API, **pinned version string** from `config.global.model_version` |
| **SDK** | `google-genai` |

**Two distinct modes.** They share a provider and, as of the matting removal,
share an input shape too — neither sends anything but the source material and
a prompt.

### Mode A — Real-photo transformation (the common path)

Input: source photograph + category/angle prompt. Gemini's job is
background synthesis, subject relighting, drop shadow, **and** background
removal — all in one call. This should be the overwhelming majority of
traffic.

**No QA gate on Mode A.** This is a deliberate, documented risk — see
`docs/decisions/0001-drop-local-matting.md` — not an oversight. If output
quality on hard cases (fine chain, transparency, specular highlight)
degrades in practice, the fallback is routing Mode A through the same QA
similarity gate Mode B uses.

### Mode B — Synthetic angle generation (the risky path)

Input: category reference image matrix + angle prompt. **No source photograph.**
Gemini is producing a novel view of an object it has not seen from that
direction.

Mode B will invent product detail — extra chain links, wrong prong counts,
invented facet geometry — and it does so silently, returning a 200 with a
beautiful wrong image. This is why Mode B is gated behind
`synthetic_allowed`, flagged in the response, and mandatorily QA-checked.
Never chain Mode B off another generated image; hallucination compounds.

### Mode C — Background operations (Phase 15)

| | |
| :--- | :--- |
| **Trigger** | Same as Mode A/B — sub-job enters `GENERATING` |
| **Where** | Celery `io` queue, `app/workers/background.py` via `app/providers/gemini.py` — **the same `GeminiProvider` class**, unmodified |
| **Input** | One uploaded photo. For `BACKGROUND_REPLACEMENT`, either the pinned preset's own prompt is appended to the operation prompt, **or** — if a custom background photo was uploaded instead (`sub_jobs.background_asset_id`) — that photo's bytes are appended as a second reference image and `custom_background_prompt` is appended to the operation prompt instead (`app/services/background_service.py::_resolve_prompt`/`process`) |
| **Output** | Product on a flat/solid background — no alpha channel, same `image/jpeg`/`image/png` shape Mode A already produces. "Removal" means standardisation, not a transparent cutout — see `docs/decisions/0002-background-removal-approach.md` for why the Gemini API path can't produce one |

Decided directly rather than spiked over real client pieces (no real `GEMINI_API_KEY`
or client photos existed to run the spike this phase's own Step 1 called for) — the
client accepted the no-transparency tradeoff up front. Reuses `rate_limiter`
unmodified: these calls compete with Mode A/B for the same global
`GEMINI_RATE_LIMIT_PER_MINUTE` window.

**Unlike Mode A, always QA-gated** (see Call site 2's background note below) — a
drifted or partly-altered product is unacceptable when the cutout/composite *is* the
product being sold.

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`, `seed`.
Without all three, a bad output cannot be reproduced or debugged.

### Mode D — Companion-piece generation (MATCH, Phase 18)

| | |
| :--- | :--- |
| **Trigger** | Same as Mode A/B/C — sub-job enters `GENERATING` |
| **Where** | Celery `io` queue, `app/workers/match.py` via `app/providers/gemini.py` — **the same `GeminiProvider` class**, unmodified |
| **Input** | One uploaded photo (the style reference) + `{target_category}` from the request, substituted into `config.global.operations.MATCH.prompt` at call time by `app/services/job_service.py::resolve_match_prompt` (`str.format`, fails loud with `KeyError` if the template ever references an unsupplied placeholder — never ships a literal `{...}` to Gemini) |
| **Output** | A standalone studio product photograph of a **different, new** item — a companion piece, not the input piece |

**The source image is a style reference, not the subject being transformed —
this is the key difference from Modes A-C.** Mode A/B transform or invent a
view of *the same physical piece*; Mode C's cutout/composite *is* the
product being sold, byte-for-byte the same subject on a new backdrop. MATCH's
output is a **different physical piece** that doesn't exist yet — Gemini is
asked to match metal tone, stone type/cut, and design language, then render
a new, standalone item. Because the output is never supposed to be the same
object as the input, none of Modes A-C's compositing or subject-preservation
logic applies here: there's nothing to preserve outside a mask (no mask
exists) and no "did this drift from the source" question to ask, because
drifting from the source — while staying stylistically consistent with it —
is the entire point.

**No QA gate.** This is also why MATCH ships straight to `COMPLETED` on a
successful provider call, unlike Mode C's unconditional `QA_REVIEW`: the
existing `GeminiQaProvider` gate is a subject-preservation similarity
check ("did the output stay faithful to the input"), and that question is
the wrong one for MATCH's output by design. A stylistic-consistency judge
would be a different tool than what Phase 9 built, and this phase
deliberately didn't build one speculatively — see
`docs/business-rules.md` §7. Same "let data decide" posture the "Deferred
to v3" table already uses elsewhere in this project.

**Reuses `rate_limiter` unmodified** — MATCH's calls compete with Modes
A/B/C for the same global `GEMINI_RATE_LIMIT_PER_MINUTE` window, no
separate budget.

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`,
`seed` — same fields, same reason, as Mode C.

### Mode E — Masked gemstone recolor (RECOLOR, Phase 19)

| | |
| :--- | :--- |
| **Trigger** | Same as Mode A/B/C/D — sub-job enters `GENERATING` |
| **Where** | Celery `io` queue, `app/workers/recolor.py` via `app/providers/gemini.py` — **the same `GeminiProvider` class**, unmodified |
| **Input** | One image: a solid-magenta overlay burned onto the source photo through the (eroded) mask — never the raw source, never the mask itself. Built by `app/services/recolor_service.py::_build_overlay` |
| **Output** | The provider's raw response is **not** the client-facing artifact — see below |

**Unlike every other mode, the client-facing output is not the provider's raw
response.** Confirmed from `app/providers/gemini.py::_call_api`: the Gemini API
takes only `Part.from_text` and `Part.from_bytes` — there is no mask parameter,
no alpha-channel input. So the mask does its real work in two places, neither of
which is "passed as a mask":

1. **Before the call**, it's burned into a colour overlay (a solid magenta fill
   over the eroded mask region) that becomes the single image sent to the
   provider, alongside a prompt referencing "the region marked in magenta."
2. **After the call**, it drives a server-side compositing step
   (`app/services/recolor_service.py::_composite_result`) that discards
   everything the provider changed outside a *feathered* version of the mask —
   erosion and feathering are two separate passes for two separate purposes (see
   `docs/business-rules.md` §15), not the same blur applied twice. Everywhere the
   feathered mask is 0, the stored `OUTPUT` asset's pixel is the original
   source's pixel, exactly.

This is the direct architectural consequence of the same fact Call site 1's own
constraint note above already states — no mask parameter exists — applied to a
case (RECOLOR) that, unlike Modes A-D, genuinely needs to preserve part of an
image byte-for-byte while regenerating another part.

**No QA gate**, for a third, distinct reason from MATCH's — see
`docs/business-rules.md` §7's RECOLOR note and §15.

**Reuses `rate_limiter` unmodified** — same global `GEMINI_RATE_LIMIT_PER_MINUTE`
window every other mode competes for.

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`,
`seed` — same fields, same reason, as every other mode.

**Mask-conveyance strategy (the colour overlay) is unvalidated against a real
model call** — no real `GEMINI_API_KEY` exists in this environment, same gap
every phase since 6 has hit. If a future session with a real key finds this
doesn't reliably confine edits to the marked region on real jewelry macro
photography, that correction belongs here and in
`phases/phase-19-recolor.md`'s own reality-check section — not silently in code.

**The overlay built here is downscaled to `settings.WORKING_MAX_EDGE` before
it's sent to Gemini** (post-Phase-20 incident fix, 2026-08-24 — see
`docs/business-rules.md` §15 and `app/config.py`'s own note). This is Gemini's
input only, never the client-facing artifact, so it costs nothing
correctness-wise; the final compositing step still uses the source at its real,
full resolution.

**Rate limiting (Phase 6, `app/services/rate_limiter.py`).** All provider
calls pass through a **Redis fixed-window counter**
(`provider:gemini:tokens:{minute-window}`) shared across every worker,
capacity from `GEMINI_RATE_LIMIT_PER_MINUTE`. Four sub-tasks per job
multiplied across concurrent jobs will otherwise burst straight into 429s,
which under fail-fast converts directly into `PARTIAL_SUCCESS` for every
in-flight job at once. A window at capacity is treated identically to a live
429 from Gemini — same `RATE_LIMITED` failure path, not a special case.

**Abstraction boundary.** All calls go through `GenerationProvider` in
`app/providers/base.py`. Task bodies must not import `google-genai` — only
`app/providers/gemini.py` does, and even there the import is deferred inside
`_call_api` so unit tests never touch it. A second provider is deferred to
v3, but the seam costs an afternoon now and a rewrite later.

**Failure modes:**

| Symptom | Class | Internal backoff |
| :--- | :--- | :--- |
| HTTP 429 / local rate-limit window exhausted | `RATE_LIMITED` | Yes, 3 attempts |
| HTTP 5xx | `TRANSIENT_PROVIDER` | Yes, 3 attempts |
| Timeout / connection reset | `TRANSIENT_NETWORK` | Yes, 3 attempts |
| Safety refusal / empty candidate | `SAFETY_REFUSAL` → `REJECTED` | **No** |
| Malformed response | `INTERNAL` | No |

**Retry implementation (Phase 6):** the "3 attempts" above is a tight
in-process loop inside `app/services/generation_service.py`
(`transform_photo`), not Celery-level `autoretry_for`/`retry_backoff` — no
exponential/jitter delay between in-process attempts yet. Chosen for
deterministic testability under `task_always_eager`; revisit as real Celery
retry if backoff timing between attempts becomes operationally necessary.

A safety refusal is deterministic. Retrying it wastes money and time, and the
ERP must not offer a retry button for it.

**Cost.** Every call writes a `cost_events` row before the result is
evaluated — including calls that end in refusal. A refused generation is
still billed.

### Mode F — Two-piece masked merge (MIX, Phase 20)

| | |
| :--- | :--- |
| **Trigger** | Same as Mode A/B/C/D/E — sub-job enters `GENERATING` |
| **Where** | Celery `io` queue, `app/workers/mix.py` via `app/providers/gemini.py` — **the same `GeminiProvider` class**, unmodified |
| **Input** | One image: a solid-magenta *seam-band* overlay burned onto a deterministically-assembled `rough_composite`, not onto either raw source. Built by `app/services/mix_service.py::_build_rough_composite` (no model call) then `_build_seam_overlay` |
| **Output** | The provider's raw response is **not** the client-facing artifact, same as Mode E — see below |

**Unlike Mode E, the step before the provider call does real deterministic image
assembly, not just an overlay burn.** Mode E's overlay is the *source* photo with a
colour fill over the edit region — the underlying image content is unchanged. MIX's
overlay is built from `rough_composite`, an image that does not exist anywhere
until this phase's own new pre-provider step constructs it: region B (the piece
being grafted from) is cropped to its mask's bounding box, scaled (aspect ratio not
preserved) to fit region A's bounding box, and pasted onto image A via mask A's own
silhouette as the paste alpha. **No model call is involved in placement at all** —
only the visible seam between the two pieces is Gemini's job, confined to a ring
(`MIX_SEAM_BAND_PX` pixels wide) around the graft boundary rather than the mask's
full interior the way Mode E's edit region is. This split exists because cross-image
spatial reasoning — correctly placing content from one photo's frame into another's
— is the weakest capability in play for any current-generation image model; asking
Gemini to do placement *and* blending in one call risks the graft landing in the
wrong position or at the wrong scale with no ground truth to check it against.

1. **Before the call**, `rough_composite` is built deterministically (Pillow only),
   then a seam-band ring (`dilate(mask_a, band_px) - erode(mask_a, band_px)`) is
   burned into a magenta overlay on top of it — same hard-edged-overlay mechanism
   Mode E established, applied to a ring instead of a filled region.
2. **After the call**, the same seam-band mask, feathered by `MASK_FEATHER_PX`
   (Mode E's own existing setting, reused rather than duplicated), drives a
   server-side compositing step (`app/services/mix_service.py::_composite_seam_result`)
   that discards everything the provider changed outside the seam. Everywhere the
   feathered seam-band mask is 0 — both the untouched rest of image A *and* the
   already-correct interior of the graft — the stored `OUTPUT` asset's pixel is
   `rough_composite`'s own pixel, exactly.

This is the direct architectural consequence of the same fact Call site 1's own
constraint note above already states — no mask parameter exists — applied to a case
(MIX) that, unlike every mode before it, needs to assemble content from **two**
independent source images before any provider involvement, then bound the provider's
influence to a boundary between them rather than a single edit region.

**No QA gate**, for a fourth, distinct reason from MATCH's and RECOLOR's — see
`docs/business-rules.md` §7's MIX note and §16.

**Reuses `rate_limiter` unmodified** — same global `GEMINI_RATE_LIMIT_PER_MINUTE`
window every other mode competes for.

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`,
`seed` — same fields, same reason, as every other mode.

**Both the non-aspect-preserving scale-to-fit and the seam-only refinement strategy
are unvalidated against a real model call** — no real `GEMINI_API_KEY` exists in
this environment, same gap every phase since 6 has hit. If a future session with a
real key finds either doesn't hold up on real jewelry macro photography, that
correction belongs here and in `phases/phase-20-mix.md`'s own reality-check section
— not silently in code.

**Known limitation, found while building this phase's own central pixel-identity
test, not a theoretical concern:** for a masked region narrower than roughly
`2 * (MIX_SEAM_BAND_PX + MASK_FEATHER_PX)` in either dimension, the post-call
Gaussian feather can bleed through the graft's interior from both sides of the
seam-band ring at once — see `app/services/mix_service.py::_seam_band_mask`'s own
docstring and `docs/business-rules.md` §16.

**The seam overlay built here is downscaled to `settings.WORKING_MAX_EDGE`
before it's sent to Gemini, same fix as Mode E's** (post-Phase-20 incident,
2026-08-24 — see `docs/business-rules.md` §16 and `app/config.py`'s own
note). `_build_rough_composite`'s *output* was, and still is, **not**
downscaled — it's the base the final compositing step composites back onto,
not a throwaway Gemini input. At the time this left the function decoding
four full-resolution images at once, flagged as MIX's own remaining memory
hotspot. **2026-08-25 follow-up:** source B and mask B (the two inputs that
get cropped-then-resized into region A's bounding box regardless) are now
downscaled to `WORKING_MAX_EDGE` *before* the crop, and a redundant
full-resolution `.copy()` of source A was removed — source A/mask A stay
full resolution as before, but the function now holds two uncapped
full-resolution buffers instead of four. See
`app/services/mix_service.py::_build_rough_composite`'s own docstring.

---

## Call site 2 — QA similarity gate

| | |
| :--- | :--- |
| **Trigger** | A `SYNTHETIC` sub-job completes generation, **or** any background-operation sub-job completes generation (Phase 15 — unconditional, not gated on source type). Real-photo *angle* sub-jobs still have no QA gate (see above). |
| **Where** | Celery `io` queue, `app/workers/qa.py::score_similarity` (angles) / `score_background` (Phase 15), dispatched by `app/workers/generation.py` / `app/workers/background.py` respectively, right after a `QA_REVIEW`-landing commit (same placement rule, Phase 9). |
| **Model** | LLM-judged similarity via Gemini (`app/providers/gemini_qa.py::GeminiQaProvider`) — decided in Phase 9, per this doc's own prior note that it was the likely default. No dedicated embedding model was added; revisit only if the judge proves unreliable in practice. **Same class, unmodified, for both angles and background operations.** |
| **Input** | Angles: reference image(s) for the category + the generated output. **Background operations (Phase 15):** the **input photo itself** as the sole reference + the generated output — `app/services/qa_service.py::score_background_operation`. The subject is meant to be identical; "same object, new background" is what's judged, not "novel view of the same object." |
| **Output** | `qa_score` ∈ [0, 1] written to `sub_jobs`, plus `qa_status` |

Threshold from `config.global.qa_similarity_threshold` for angles, default `0.82` — **a
placeholder until calibrated against real client pieces.** Calibrate by
scoring known-good and known-bad outputs and picking the threshold that
separates them, not by intuition. **Still not calibrated as of Phase 9** —
no real client pieces exist in this environment (same situation Phase 6 hit
with `GEMINI_API_KEY`), same as roadmap open decision #8. Background operations use
a **separate, independently-tunable** `config.global.background_qa_similarity_threshold`
(migration 0010, Phase 15), placeholder `0.92` — deliberately higher, also
uncalibrated, same gap.

Below threshold → `QA_REVIEW` and the human queue (`qa_status: FLAGGED`).
Above → `COMPLETED` (`qa_status: PASSED`). A QA provider failure (timeout,
malformed response) is treated the same as below-threshold — `QA_REVIEW`
+ `FLAGGED`, `qa_score: NULL` — never auto-`COMPLETED` and never
auto-`REJECTED`. Fail open to a human, never to an unscored pass — see
`phases/phase-9-qa-gate.md`'s reality-check section; this exact case wasn't
specified anywhere before this phase. Both `score_similarity` and
`score_background` (Phase 15) share this exact branching logic via a
private `qa_service._score_and_apply` helper — only how threshold/
reference images are resolved differs between them.

This is the **only** mechanism in the system that catches a silent failure
on synthetic angles — and, since Phase 15, on background operations too. A
drifted or partly-eaten product from a background operation would
otherwise return a 200 with a beautiful wrong result, the same failure
shape Mode B's hallucination risk has always had.

**Not billed.** No `cost_events` row is written for a QA call — neither
`docs/business-rules.md` §10 nor this doc ever described QA scoring as a
billed operation, so Phase 9 didn't invent one. Revisit in Phase 11 if real
usage data shows this matters.

---

## Testing rules

- **Never call the live Gemini API in CI.** Use recorded response fixtures in
  `tests/fixtures/gemini/`, covering: success, 429, 5xx, timeout, safety
  refusal, and malformed response. QA-scoring fixtures live in
  `tests/fixtures/qa/`: `high_similarity.json`, `low_similarity.json`,
  `malformed.json` — `GeminiQaProvider._call_api` is monkeypatched the same
  way `GeminiProvider._call_api` is.
- Celery logic is tested with `task_always_eager`; queue routing is tested
  separately in integration.

---

## Model version pinning

| Model | Pinned where | Changed how |
| :--- | :--- | :--- |
| Gemini image model | `config.global.model_version` (Sheets → config version) | New config version, deliberate |
| QA embedding/judge model | `QA_MODEL_ID` env var | Deploy, deliberate |

Never call a floating alias. A silent upstream model update shifts the visual
style of the entire catalog overnight, and without a recorded version you
will not be able to prove it happened.
