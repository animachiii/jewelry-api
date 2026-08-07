# AI Integration

Every place a model runs. Two call sites, one queue, one abstraction boundary.

**2026-08-07:** local matte extraction (BiRefNet/RMBG-2.0) was dropped —
see `docs/decisions/0001-drop-local-matting.md`. Real-photo angles now go
straight to Gemini, same shape as synthetic angles. There is no `gpu`
queue anymore.

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

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`, `seed`.
Without all three, a bad output cannot be reproduced or debugged.

**Rate limiting.** All provider calls pass through a **Redis token bucket**
(`provider:gemini:tokens`) shared across every worker. Four sub-tasks per job
multiplied across concurrent jobs will otherwise burst straight into 429s,
which under fail-fast converts directly into `PARTIAL_SUCCESS` for every
in-flight job at once.

**Abstraction boundary.** All calls go through `GenerationProvider` in
`app/providers/base.py`. Task bodies must not import `google-genai`. A second
provider is deferred to v3, but the seam costs an afternoon now and a
rewrite later.

**Failure modes:**

| Symptom | Class | Internal backoff |
| :--- | :--- | :--- |
| HTTP 429 | `RATE_LIMITED` | Yes, 3 attempts |
| HTTP 5xx | `TRANSIENT_PROVIDER` | Yes, 3 attempts |
| Timeout / connection reset | `TRANSIENT_NETWORK` | Yes, 3 attempts |
| Safety refusal / empty candidate | `SAFETY_REFUSAL` → `REJECTED` | **No** |
| Malformed response | `INTERNAL` | No |

A safety refusal is deterministic. Retrying it wastes money and time, and the
ERP must not offer a retry button for it.

**Cost.** Every call writes a `cost_events` row before the result is
evaluated — including calls that end in refusal. A refused generation is
still billed.

---

## Call site 2 — QA similarity gate

| | |
| :--- | :--- |
| **Trigger** | A `SYNTHETIC` sub-job completes generation. Real-photo angles have no QA gate (see above). |
| **Where** | Celery `io` queue, `app/workers/qa.py` |
| **Model** | Perceptual embedding similarity — candidate models evaluated in Phase 9. Given the direction in `docs/decisions/0001-drop-local-matting.md`, an LLM-judged similarity check (Gemini) is the likely default; a dedicated embedding model is the fallback if that proves unreliable. Undecided until Phase 9. |
| **Input** | Reference image(s) for the category + the generated output |
| **Output** | `qa_score` ∈ [0, 1] written to `sub_jobs`, plus `qa_status` |

Threshold from `config.global.qa_similarity_threshold`, default `0.82` — **a
placeholder until calibrated against real client pieces.** Calibrate by
scoring known-good and known-bad outputs and picking the threshold that
separates them, not by intuition.

Below threshold → `QA_REVIEW` and the human queue. Above → `COMPLETED`.

This is the **only** mechanism in the system that catches a silent failure
on synthetic angles. Fail-fast, partial success, and retry all handle loud
failures — exceptions, non-200s, timeouts. A hallucinated angle throws
nothing. If this gate is weak, nothing else catches it for Mode B.

---

## Testing rules

- **Never call the live Gemini API in CI.** Use recorded response fixtures in
  `tests/fixtures/gemini/`, covering: success, 429, 5xx, timeout, safety
  refusal, and malformed response.
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
