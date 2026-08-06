# AI Integration

Every place a model runs. Three call sites, two queues, one abstraction boundary.

---

## Call site 1 — Alpha matte extraction

| | |
| :--- | :--- |
| **Trigger** | Sub-job enters `MATTING`. Skipped entirely for `SYNTHETIC` sub-jobs. |
| **Where** | Celery `gpu` queue, `app/workers/matting.py` |
| **Model** | `BiRefNet-matting` (MIT) — pending Phase 0b benchmark against `briaai/RMBG-2.0` |
| **Runtime** | PyTorch + transformers, CUDA, fp16 |
| **Input** | Source photograph from `jewelry-inputs`, downloaded to a temp path |
| **Output** | 8-bit single-channel PNG alpha matte → `jewelry-mattes`, row in `assets` |
| **Latency** | ~0.4–1.2 s per image at 1024px on a mid-range GPU (measure in Phase 0b) |

**Licensing — read before changing this.** `briaai/RMBG-2.0` ships under **CC BY-NC 4.0**.
This is a commercial project. Using RMBG-2.0 in production requires a paid Bria agreement.
BiRefNet is MIT and is the same architectural lineage — RMBG-2.0 is built on the BiRefNet
architecture. Default to BiRefNet unless the Phase 0b benchmark shows an unacceptable
quality gap and the client buys the Bria license.

**Loading rule.** The model loads **once per worker process**, in a
`worker_process_init` signal handler, into a module-level singleton. Never inside a task.
Under Celery prefork, in-task loading multiplies VRAM by the child count and OOMs at a
concurrency nobody documented.

**Concurrency.** `gpu` queue concurrency is capped by measured VRAM headroom, typically
1–2 per card. This is set from an env var, not guessed.

**Failure modes:**

| Symptom | Class |
| :--- | :--- |
| Corrupt or unreadable image | `INVALID_INPUT` |
| Matte is empty or near-empty (subject not found) | `INVALID_INPUT` |
| CUDA OOM | `INTERNAL` — retryable, indicates concurrency is misconfigured |
| Model file missing at boot | Worker fails to start. Fail loudly at init, not per task. |

**Quality note.** Jewelry is the hard case for matting: chain links a few pixels wide,
faceted transparency, specular highlights that segmentation models read as background.
The Phase 0b benchmark must use real client pieces including at least one fine chain, one
transparent gemstone, and one high-polish metal surface.

---

## Call site 2 — Image generation

| | |
| :--- | :--- |
| **Trigger** | Sub-job enters `GENERATING` |
| **Where** | Celery `io` queue, `app/workers/generation.py` via `app/providers/gemini.py` |
| **Model** | Gemini Image API, **pinned version string** from `config.global.model_version` |
| **SDK** | `google-genai` |

**Two distinct modes.** They share a provider but not a prompt strategy.

### Mode A — Real-photo transformation (the common path)

Input: source photograph + alpha matte + category/angle prompt.
Gemini's job is background synthesis, subject relighting, and drop shadow. **The subject
pixels are anchored by the matte.** This is a well-behaved, high-fidelity operation and
should be the overwhelming majority of traffic.

### Mode B — Synthetic angle generation (the risky path)

Input: category reference image matrix + angle prompt. **No source photograph.**
Gemini is producing a novel view of an object it has not seen from that direction.

Mode B will invent product detail — extra chain links, wrong prong counts, invented facet
geometry — and it does so silently, returning a 200 with a beautiful wrong image. This is
why Mode B is gated behind `synthetic_allowed`, flagged in the response, and mandatorily
QA-checked. Never chain Mode B off another generated image; hallucination compounds.

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`, `seed`.
Without all three, a bad output cannot be reproduced or debugged.

**Rate limiting.** All provider calls pass through a **Redis token bucket**
(`provider:gemini:tokens`) shared across every worker. Four sub-tasks per job multiplied
across concurrent jobs will otherwise burst straight into 429s, which under fail-fast
converts directly into `PARTIAL_SUCCESS` for every in-flight job at once.

**Abstraction boundary.** All calls go through `GenerationProvider` in
`app/providers/base.py`. Task bodies must not import `google-genai`. A second provider is
deferred to v3, but the seam costs an afternoon now and a rewrite later.

**Failure modes:**

| Symptom | Class | Internal backoff |
| :--- | :--- | :--- |
| HTTP 429 | `RATE_LIMITED` | Yes, 3 attempts |
| HTTP 5xx | `TRANSIENT_PROVIDER` | Yes, 3 attempts |
| Timeout / connection reset | `TRANSIENT_NETWORK` | Yes, 3 attempts |
| Safety refusal / empty candidate | `SAFETY_REFUSAL` → `REJECTED` | **No** |
| Malformed response | `INTERNAL` | No |

A safety refusal is deterministic. Retrying it wastes money and time, and the ERP must not
offer a retry button for it.

**Cost.** Every call writes a `cost_events` row before the result is evaluated — including
calls that end in refusal. A refused generation is still billed.

---

## Call site 3 — QA similarity gate

| | |
| :--- | :--- |
| **Trigger** | A `SYNTHETIC` sub-job completes generation. Real-photo angles skip this. |
| **Where** | Celery `io` queue (or `gpu` if the embedding model is GPU-backed), `app/workers/qa.py` |
| **Model** | Perceptual embedding similarity — candidate models evaluated in Phase 9 |
| **Input** | Reference image(s) for the category + the generated output |
| **Output** | `qa_score` ∈ [0, 1] written to `sub_jobs`, plus `qa_status` |

Threshold from `config.global.qa_similarity_threshold`, default `0.82` — **a placeholder
until calibrated against real client pieces.** Calibrate by scoring known-good and
known-bad outputs and picking the threshold that separates them, not by intuition.

Below threshold → `QA_REVIEW` and the human queue. Above → `COMPLETED`.

This is the **only** mechanism in the system that catches a silent failure. Fail-fast,
partial success, and retry all handle loud failures — exceptions, non-200s, timeouts. A
hallucinated angle throws nothing. If this gate is weak, nothing else catches it.

---

## Testing rules

- **Never call the live Gemini API in CI.** Use recorded response fixtures in
  `tests/fixtures/gemini/`, covering: success, 429, 5xx, timeout, safety refusal, and
  malformed response.
- **Never load the matting model in unit tests.** Mock the singleton. Real model
  execution belongs in a marked integration test that is skipped without a GPU.
- Celery logic is tested with `task_always_eager`; queue routing and concurrency are
  tested separately in integration.

---

## Model version pinning

| Model | Pinned where | Changed how |
| :--- | :--- | :--- |
| Gemini image model | `config.global.model_version` (Sheets → config version) | New config version, deliberate |
| Matting model | `MATTING_MODEL_ID` env var + pinned revision hash | Deploy, deliberate |
| QA embedding model | `QA_MODEL_ID` env var | Deploy, deliberate |

Never call a floating alias. A silent upstream model update shifts the visual style of the
entire catalog overnight, and without a recorded version you will not be able to prove it
happened.
