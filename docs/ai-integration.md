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
| **Input** | One uploaded photo. For `BACKGROUND_REPLACEMENT`, the pinned preset's own prompt is appended to the operation prompt (`app/services/background_service.py::_resolve_prompt`) |
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
