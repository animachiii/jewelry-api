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

### Mode F — Two-piece combination (MIX, Phase 20; rewritten generative 2026-08-31)

| | |
| :--- | :--- |
| **Trigger** | Same as Mode A/B/C/D/E — sub-job enters `GENERATING` |
| **Where** | Celery `io` queue, `app/workers/mix.py` via `app/providers/gemini.py` — **the same `GeminiProvider` class**, unmodified |
| **Input** | **Two** images: each uploaded photo with its client-painted region marked in its own colour — magenta for the primary, cyan for the secondary. Built by `app/services/mix_service.py::_build_highlight`, called once per pair, and passed in that order |
| **Output** | The provider's raw response, stored unmodified — same as Modes A-D, and **no longer** like Mode E |

**This section was rewritten on 2026-08-31; the pipeline it used to describe no
longer exists.** Mode F was previously the only mode that did deterministic
image *assembly* before its provider call: it built a `rough_composite` by
cropping the secondary photo's masked region, scaling it into the primary's
masked region and pasting it through the intersection of both silhouettes, then
asked Gemini to blend a ring around the resulting seam, then composited the
response back so everything outside that ring stayed byte-identical to the
composite. `docs/business-rules.md` §16 has the full accounting of why that was
abandoned — in short, fitting one painted silhouette into another is the wrong
operation for the request, and three live client jobs demonstrated it.

**What Mode F does now** is the simplest shape of any masked operation:

1. **Before the call**, each photo is marked independently — a faint tint
   (`_HIGHLIGHT_TINT_ALPHA`) plus a solid contour (`_HIGHLIGHT_OUTLINE_PX`) in
   that pair's colour, over the client's painted region only. Nothing is
   cropped, scaled, relocated or intersected. The rest of each photo is left
   intact, because the surrounding piece is context the design depends on.
2. **The call** sends both marked photos as reference images
   (`GeminiProvider.generate` has always accepted a `list[bytes]` — no provider
   change was needed, same as Phase 15's custom-background addition) alongside a
   prompt that names both colours and asks for one new piece combining the two
   marked elements.
3. **After the call** — nothing. The response is the OUTPUT asset.

**Why translucent marks, unlike Mode E's opaque magenta fill.** RECOLOR paints
its region opaque because it wants that region *replaced*; hiding what is under
the fill costs nothing. MIX's marked region is precisely what the model must
*reproduce*, so covering it would defeat the purpose. The contour carries the
identification; the tint only disambiguates inside from outside on concave
shapes. Its value was measured rather than guessed — at 0.30 the cyan mark
turned a real client photo's gold bands visibly green, which risks the model
rendering green enamel; see the constant's own note in `mix_service.py`.

**Why two colours.** With two reference images, the prompt has to be able to say
*which* element belongs to *which* piece. Magenta and cyan are both unreachable
by real jewelry — gold, silver, ruby, emerald, sapphire and pearl are far from
both in hue — so a mark can never be mistaken for the piece's own material. The
pairing is a contract between `mix_service.process`'s argument order and
migration `0020`'s prompt text; changing one without the other still produces a
successful call that describes the wrong image, which is why
`test_mix_sends_two_colour_marked_reference_images_in_documented_order` pins it.

**No QA gate**, and since this rewrite the reason is MATCH's rather than a
distinct one — the output is supposed to differ from both inputs. See
`docs/business-rules.md` §7's MIX note and §16.

**Mode F now carries Mode B's hallucination risk, and nothing catches it.** The
output is a design concept: Gemini can invent prong counts, facet geometry and
chain links neither physical piece has. Mode B is gated behind
`synthetic_allowed`, flagged `synthetic: true` in the response, and mandatorily
QA-checked for exactly this reason. Mode F has none of those three controls.
That gap is deliberate and accepted (the client wants a mockup, not a
photograph), but `docs/business-rules.md` §16 records the response-flagging half
of it as a genuine open item, not a solved one.

**Reuses `rate_limiter` unmodified** — same global `GEMINI_RATE_LIMIT_PER_MINUTE`
window every other mode competes for.

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`,
`seed` — same fields, same reason, as every other mode.

**Memory.** Both reference images are built through
`mix_service._load_downscaled` at `settings.WORKING_MAX_EDGE`, preserving the
2026-08-27 OOM fix (a real 12.6 MP client upload needed ~187 MB against ~160 MB
of headroom and was SIGKILLed before every Gemini attempt, `attempt_count` never
leaving `0`). This pipeline holds strictly less than the old one did — two
downscaled images rather than a rough composite plus a seam overlay plus a
provider output plus a final composite. **Mode F's output resolution is now
whatever Gemini returns**, no longer capped by our own canvas. Mode E (RECOLOR)
still composites at full resolution and retains the same exposure — unchanged,
still a known risk.

**Unvalidated against a real model call at the time of writing.** The
highlight-plus-prompt strategy carries the same status Phase 20's seam-band
strategy did — with one difference worth acting on: a real `GEMINI_API_KEY` now
exists in the deployed environment, and job `f9768456`'s four stored input
assets are the exact case that motivated this rewrite. Validating against them
is a real, available step, not a blocked one. A correction belongs here and in
`docs/business-rules.md` §16, not silently in code.

---

## Call site 2 — QA similarity gate

| | |
| :--- | :--- |
| **Trigger** | A `SYNTHETIC` sub-job completes generation, **or** any background-operation sub-job completes generation (Phase 15 — unconditional, not gated on source type). Real-photo *angle* sub-jobs still have no QA gate (see above). |
| **Where** | Celery `io` queue, `app/workers/qa.py::score_similarity` (angles) / `score_background` (Phase 15), dispatched by `app/workers/generation.py` / `app/workers/background.py` respectively, right after a `QA_REVIEW`-landing commit (same placement rule, Phase 9). |
| **Model** | LLM-judged similarity via Gemini (`app/providers/gemini_qa.py::GeminiQaProvider`) — decided in Phase 9, per this doc's own prior note that it was the likely default. No dedicated embedding model was added; revisit only if the judge proves unreliable in practice. **Same class, unmodified, for both angles and background operations.** |
| **Input** | Angles: reference image(s) for the category + the generated output. **Background operations (Phase 15):** the **input photo itself** as the sole reference + the generated output — `app/services/qa_service.py::score_background_operation`. The subject is meant to be identical; "same object, new background" is what's judged, not "novel view of the same object." |
| **Prompt** | **Two prompts, one per call site, chosen by the caller (2026-08-28).** Angles get `PIECE_IDENTITY_JUDGE_PROMPT` ("is this the same piece"); background operations get `SUBJECT_PRESERVATION_JUDGE_PROMPT`, which explicitly lists background/props/hands/tags/crop/pose differences as **intended** and tells the judge to score piece identity alone. `QaProvider.score` takes `prompt` as a required keyword argument with **no default** — see the failure below. |
| **Output** | `qa_score` ∈ [0, 1] written to `sub_jobs`, plus `qa_status`, plus the judge's own `reasoning` recorded on the `QA_SCORED` `job_events` row |

**Why there are two prompts (live finding, 2026-08-28).** Until this date both
call sites shared one prompt — the piece-identity one, written for Mode B's
catalogue-reference matrix. Applied to a background operation, whose reference
is the seller's raw input snapshot, it scores intended changes as evidence of a
different piece. Live sub-job `6b3eda1e` (2026-08-27) turned a bracelet draped
over a velvet pillow in someone's hand into a flawless open-bangle studio shot
— exactly what migration `0019` instructs the generator to produce — and the
judge scored it **0.0**. The better the generator obeyed `0019`, the more
reliably the judge flagged it. Post-fix production scores were bimodal
(`0.0, 0.1, 1.0, 1.0`), so `background_qa_similarity_threshold` was doing no
calibration work at all; the new prompt's scoring anchors are deliberately
top-heavy so `0.92` still reads as "the judge is confident this is the same
piece" without the threshold itself changing.

**The judge's `reasoning` is now persisted.** Both prompts have asked for it
since Phase 9 and `_parse_response` discarded it, so "why was this flagged?"
could only be answered by re-downloading both images and re-running the judge
by hand. It now lands in the `QA_SCORED` event's `detail.reasoning` (bounded to
500 chars); on a `provider_error` outcome that field carries the failure class
and message instead, since a `NULL` `qa_score` is otherwise the least
self-explanatory of the three outcomes.

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
