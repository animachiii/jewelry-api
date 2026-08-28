# Business Rules

Rules in this file are invariants. If code and this file disagree, one of them is a bug —
resolve it explicitly, never silently.

---

## 1. Angle matrix

- There are exactly four angles: `FRONT`, `SIDE`, `DIAGONAL`, `TOP`.
- There are seven jewelry categories. Which angles are enabled is **per category** and
  defined by the active config version — never hardcoded.
- A job may request between 1 and 4 angles. Zero requested angles is a `422`.
- An angle disabled for a category cannot be requested. A `422`, not a silent skip.
- `synthetic_allowed` is also per category per angle. Requesting synthetic generation for
  an angle where it is not allowed is a `422`.

**MATCH's `target_category` (§14, Phase 18)** validates against this exact same
`payload.categories` list — not a MATCH-specific category list. `operations.MATCH`
has no category enumeration of its own; category existence/active-ness is a single
source of truth shared by `/generate` and `/match` alike. The angle-enablement and
`synthetic_allowed` bullets above don't apply to MATCH (it has no angles).

**RECOLOR (§15, Phase 19) has no category_code and no angle matrix at all** — same
carve-out as background operations (§13), not MATCH's. See §15.

**MIX (§16, Phase 20) has no category_code and no angle matrix either** — same
carve-out as RECOLOR and background operations. See §16.

---

## 2. Job state machine

**Parent job:**

```
PENDING ──► PROCESSING ──► COMPLETED | PARTIAL_SUCCESS | FAILED
```

`COMPLETED`, `PARTIAL_SUCCESS`, and `FAILED` are terminal — **except** that a successful
retry can move `PARTIAL_SUCCESS` or `FAILED` back to `PROCESSING`. This is the one legal
backward transition and it exists only via the retry endpoint.

**Sub-job:**

```
PENDING ──► GENERATING ──► QA_REVIEW ──► COMPLETED
                       └──► COMPLETED  (QA not applicable)
   any ──► FAILED | REJECTED
PENDING ──► SKIPPED  (set at creation, never transitions)
```

- No `MATTING` step — see `docs/decisions/0001-drop-local-matting.md`. Every
  sub-job goes `PENDING` → `GENERATING` directly, real or synthetic.
- `QA_REVIEW` is entered **only** for `SYNTHETIC` sub-jobs, and only when the similarity
  score falls below threshold. Above threshold, go straight to `COMPLETED`.
  Real-photo (`UPLOADED`) sub-jobs have no QA gate at all — see
  `docs/ai-integration.md`.
- **Exception — background operations (§13, Phase 15):** a `BACKGROUND_REMOVAL` /
  `BACKGROUND_REPLACEMENT` sub-job is always `UPLOADED`, but unlike Mode A angle
  generation it **always** enters `QA_REVIEW` on success, never straight
  `COMPLETED` — the subject-preservation gate applies unconditionally, not only to
  `SYNTHETIC` sub-jobs.

---

## 3. Parent status computation

Recomputed after every sub-job terminal transition, inside the same transaction.
`SKIPPED` sub-jobs are excluded from all counts. Implemented in
`app/services/status_rollup.py::compute_parent_status` (Phase 2) and called from
`app/services/generation_service.py::transform_photo` (Phase 7) — per-transition,
not via a Celery chord; see `phases/phase-7-orchestration.md`'s reality-check
section for why a chord doesn't match "the same transaction" above.
`compute_parent_status` itself is operation-agnostic (pure function over
requested/succeeded/failed counts) — `app/services/background_service.py::process`
(Phase 15) calls the same function, unmodified.

Let `R` = requested (non-skipped) sub-jobs, `S` = succeeded (`COMPLETED`),
`F` = failed (`FAILED` + `REJECTED`).

| Condition | Parent status |
| :--- | :--- |
| `S + F < R` | `PROCESSING` |
| `S = R` | `COMPLETED` |
| `F = R` | `FAILED` |
| `S > 0` and `F > 0` and `S + F = R` | `PARTIAL_SUCCESS` |

`completed_at` is set on entering a terminal state and **cleared** when a retry moves the
job back to `PROCESSING`.

A single-angle job that fails is `FAILED`, not `PARTIAL_SUCCESS`. Partial success requires
at least one success and at least one failure. A background-operation job (§13) always
has `R = 1` for the same reason — `PARTIAL_SUCCESS` is unreachable for it, by
construction, not by a special case.

---

## 4. Failure classification

Every failure is classified before it is handled. This determines both retry behavior and
what the ERP shows.

| Class | Cause | Internal backoff | Client retry offered |
| :--- | :--- | :--- | :--- |
| `RATE_LIMITED` | Provider 429 | Yes — 3 attempts, exponential + jitter | Yes |
| `TRANSIENT_PROVIDER` | Provider 5xx | Yes — 3 attempts | Yes |
| `TRANSIENT_NETWORK` | Timeout, connection reset | Yes — 3 attempts | Yes |
| `INVALID_INPUT` | Corrupt image, unsupported format | No | No |
| `SAFETY_REFUSAL` | Provider declined to generate | No | No |
| `QA_REJECTED` | Similarity gate failed, or human rejected | No | No |
| `INTERNAL` | Unhandled backend exception | No | Yes |

**"Fail-fast" applies to deterministic classes only.** Internal backoff on transient
classes happens inside the sub-task and is invisible to the client — the sub-job does not
enter `FAILED` until the backoff budget is exhausted. Retrying a network blip three times
over six seconds is not a violation of fail-fast; it is what makes fail-fast tolerable.

`INVALID_INPUT`, `SAFETY_REFUSAL`, and `QA_REJECTED` set status `REJECTED`, not `FAILED`,
and `retryable: false`. The ERP must not render a retry button for these.

---

## 5. Retry rules

- Retry operates on a **single angle** (`POST /jobs/{job_id}/angles/{angle}/retry`) or,
  at the job level (`POST /jobs/{job_id}/retry`), every `FAILED` sub-job on a
  background-operation job (always exactly one, Phase 15, §13) or a MATCH job
  (0-4 of its 1-4 variants, Phase 18, §14) — all-or-nothing, see §14. Never a whole
  angle job. `POST /jobs/{job_id}/retry` returns `409` if called on an
  `ANGLE_GENERATION` job.
- Maximum **3 client-initiated retries** per sub-job (`attempt_count` ceiling). The fourth
  request returns `409`.
- Retry requires the sub-job to be in `FAILED`. `REJECTED`, `COMPLETED`, and in-flight
  states all return `409`.
- Retry requires the input asset to be unexpired. An expired input returns `409` telling
  the client to submit a new job — the image is gone and cannot be regenerated.
- Retry reuses the job's **pinned** `config_version_id`, not the currently active version.
  Angles within one job must be visually consistent; a prompt change between the original
  run and the retry would break that.
- Retry re-runs the full generation call from scratch. It does not reuse any
  artifact from the failed attempt.
- Every retry writes a new `cost_events` row. Retries cost money and must be visible in
  cost reporting.

---

## 6. Synthetic angle rules

An angle with no source photograph is `source_type: SYNTHETIC`.

- Only permitted where the category's config sets `synthetic_allowed: true`.
- Generated from the category's reference image matrix plus prompt — never from another
  angle's output. Chaining generated images compounds hallucination.
- **Every synthetic output passes the QA similarity gate before it can be `COMPLETED`.**
- The `synthetic: true` flag is always returned in the status payload. The ERP must
  visually distinguish synthetic angles from photographed ones.

The commercial reason: a generated angle can invent chain links, prong counts, or facet
geometry the physical piece does not have. Flagging is a misrepresentation control, not a
cosmetic detail.

---

## 7. QA gate

Applies to `SYNTHETIC` sub-jobs only. Real-photo angles have no QA gate — see
`docs/decisions/0001-drop-local-matting.md` for the accepted risk this carries.

| Score vs threshold | Outcome |
| :--- | :--- |
| `qa_score >= threshold` | `qa_status: PASSED`, sub-job `COMPLETED` |
| `qa_score < threshold` | `qa_status: FLAGGED`, sub-job `QA_REVIEW`, enters human queue |
| Human approves | `qa_status: PASSED`, sub-job `COMPLETED` |
| Human rejects | `qa_status: FAILED`, sub-job `REJECTED`, `failure_class: QA_REJECTED` |

Threshold comes from `config.global.qa_similarity_threshold`. Default `0.82`, to be
calibrated against real client pieces in Phase 9 — treat the default as a placeholder.

A sub-job in `QA_REVIEW` counts as neither succeeded nor failed; the parent stays
`PROCESSING`. Do not return a flagged image to the client before a human decision.

**MATCH (§14, Phase 18) has no QA gate either — a third posture, not a fourth.**
There are now exactly two kinds of sub-job with no QA gate at all (real-photo
angles, above, and MATCH) and two kinds that always get one (`SYNTHETIC` angles,
above, and background operations, §13). MATCH's omission is a **deliberate scope
decision**, not an oversight: the existing similarity gate (`GeminiQaProvider`)
answers "did the output stay faithful to the input," which is the right question
for background operations (the cutout *is* the product) but the wrong one for
MATCH, whose output is *supposed* to differ from its source — it's a different,
matching piece, not the same piece restaged. A stylistic-consistency judge would
be a different tool than what Phase 9 built, and building one speculatively,
before real output quality shows it's needed, isn't this project's posture (see
the "Deferred to v3" table in `phases/phase-roadmap.md`). MATCH ships straight to
`COMPLETED` on a successful provider call — see `app/services/match_service.py`.

**RECOLOR (§15, Phase 19) also has no QA gate — a third, distinct reason from
MATCH's, not a fourth posture, since it's still "no gate at all" alongside
real-photo angles and MATCH.** MATCH's output is *supposed* to differ from its
source. RECOLOR's output is supposed to be **provably identical to the source
outside the mask** — that's a pixel-exact compositing-correctness question, not a
perceptual-similarity question `GeminiQaProvider` is built to answer. It's
verified by a deterministic test (off-mask pixel diff), not a probabilistic model
call. RECOLOR ships straight to `COMPLETED` on successful compositing — see
`app/services/recolor_service.py`.

**MIX (§16, Phase 20) also has no QA gate — a fourth distinct reason, not a fifth
posture, since it's still "no gate at all" alongside real-photo angles, MATCH, and
RECOLOR.** RECOLOR's output is provably identical to *the original untouched
source* outside a static mask. **MIX's output is provably identical to
`rough_composite` — itself already a deterministic merge of two different pieces'
photos, not an untouched original — outside the seam band.** The correctness claim
is real and testable the same way RECOLOR's is, but the baseline it's compared
against is a synthesized intermediate, not either original photo. MIX ships
straight to `COMPLETED` on successful compositing — see
`app/services/mix_service.py`.

---

## 8. Idempotency

- `Idempotency-Key` is **required** on `POST /generate` and `POST /retry`.
- Uniqueness is scoped `(client_id, idempotency_key)`.
- A replayed key returns the original response. It does not create a job and does not
  bill the provider.
- Keys are retained 24 hours in Redis and permanently on the `jobs` row.
- The same key with a *different* payload returns `409`. Silently returning the old job
  for a different request is worse than erroring.
- `/retry`'s dedup (Phase 8) is Redis-only, 24h TTL — there is no row of its
  own to persist a durable marker on the way `jobs.payload_hash` does for
  `/generate`. A replay past that window runs as a fresh retry instead of a
  no-op; bounded by the 3-attempt ceiling, so at most one extra call on one
  angle, not a whole extra job.

---

## 9. Config rules

- Google Sheets is the authoring surface. Postgres holds the immutable record.
- A sync computes a SHA-256 over the normalized payload. **Unchanged hash creates no new
  version.**
- Exactly one `config_versions` row is active at a time.
- Every job pins `config_version_id` at creation and never re-reads live config.
- **A Sheets outage must never fail a job.** Order of resolution: Redis cache → active
  Postgres row → hard failure only if both are unavailable.
- A sync that fails validation is recorded with `sync_status: FAILED` and does **not**
  become active. The previous version stays active.

---

## 10. Cost rules

- One `cost_events` row per provider call, including failed calls that were still billed.
- `unit_cost_usd` comes from configuration, never a hardcoded constant — provider pricing
  changes and historical rows must retain the rate that applied at the time.
- Job cost = sum of its cost events, including all retries.
- Cost is recorded even when the sub-job ends `REJECTED`. A safety refusal after a billed
  call still cost money.

---

## 11. Retention

| Asset kind | Retention | Reason |
| :--- | :--- | :--- |
| `INPUT` | 90 days | Must outlive the retry window and support audit |
| `MATTE` | 30 days | Regenerable from input |
| `OUTPUT` | Indefinite (pending client policy) | Client's catalog assets |
| `MASK` | 7 days | A client-drawn artifact, not regenerable — but has no purpose once its one `RECOLOR` job is terminal, so it doesn't need `INPUT`'s 90-day retry-window justification either (Phase 19) |

Asset rows are never deleted. Set `expires_at`; storage lifecycle removes the bytes. A
row whose bytes are gone still answers "what did we produce for this SKU."

---

## 12. Client-facing URL rules

- All image URLs are **signed, 1-hour TTL**, generated fresh on every status read.
- Signed URLs are never persisted to the database and never written to logs.
- Buckets are private. There is no public read path.

---

## 13. Background operations (Phase 15)

`BACKGROUND_REMOVAL` and `BACKGROUND_REPLACEMENT` are one-image-in/one-image-out
operations independent of the four-angle flow — `POST /background/remove`,
`POST /background/replace`. See `phases/phase-15-background-operations.md` and
`docs/decisions/0002-background-removal-approach.md`.

**Operation matrix** — both go through the same `GeminiProvider` seam Mode A angle
generation uses, unmodified:

| Operation | Input | Prompt source | Output |
| :--- | :--- | :--- | :--- |
| `BACKGROUND_REMOVAL` | One uploaded photo | `config.global.operations.BACKGROUND_REMOVAL.prompt` | Product on a flat/solid background — **no alpha channel**. "Removal" means standardisation, not a transparent cutout. |
| `BACKGROUND_REPLACEMENT` | One uploaded photo + (`preset_code` **or** `background_storage_path`) | operation prompt + either the pinned preset's own prompt, or (custom background) `config.global.operations.BACKGROUND_REPLACEMENT.custom_background_prompt` | Product on the requested backdrop |

**Custom-background compositing** (added 2026-08-13): the subject-preservation
QA gate above still applies unconditionally to a custom-background job exactly
as it does to a preset-based one, and its reference is still the **product**
input photo, never the background photo. `custom_background_prompt` is
placeholder/uncalibrated content, same status as `qa_similarity_threshold` and
`background_qa_similarity_threshold`.

- Category rules (§1) do not apply — a background job has `category_code: NULL`
  (migration 0008) and no angle (`sub_jobs.angle IS NULL`, migration 0006).
- `requested_angles` is always `1` for a background job — see §3's note. This is why
  `PARTIAL_SUCCESS` is structurally unreachable for one, not a gap in the rollup logic.
- `POST /background/replace` requires exactly one of `preset_code` /
  `background_storage_path` — `422 VALIDATION_ERROR` if both or neither are given. When
  `preset_code` is given it must name an **active** preset in the pinned config version
  (`GET /config`'s `background_presets`) — `422 PRESET_NOT_FOUND` / `422 PRESET_INACTIVE`
  otherwise. This check does not apply when `background_storage_path` is given instead.
- `operations.<OP>.enabled` gates both routes — `422 OPERATION_DISABLED` if `false` or
  absent.
- `unit_cost_usd` resolves per-operation
  (`config.global.operations.<OP>.unit_cost_usd`), falling back to
  `config.global.unit_cost_usd` when the operation doesn't set its own — same rule
  §10 states for angle generation, never a hardcoded rate.

**Subject-preservation QA gate.** Unlike Mode A angle generation (§6/§7: no QA gate on
real-photo angles, an accepted risk), a background operation's success **always**
enters `QA_REVIEW` — the cutout/composite *is* the product, so an unchecked drift is
unacceptable here in a way it wasn't for Mode A. Reuses Phase 9's QA machinery
(`app/services/qa_service.py::score_background_operation`), but the reference is the
**input photo itself**, not a category reference matrix — "same object, new
background" is what's being judged, not "novel view of the same object." Threshold is
`config.global.background_qa_similarity_threshold` (migration 0010), a separate,
independently-tunable value from `qa_similarity_threshold` and expected higher
(placeholder `0.92`, uncalibrated — same status as `qa_similarity_threshold` itself).
Flagged items appear in `GET /qa/review-queue` with `operation` set and
`angle`/`category_code` `null`.

**The judge asks a different question here than it does for a synthetic angle
(2026-08-28).** A background operation is *instructed* by migration `0019` to
strip hands, tags, props and packaging and to produce a clean e-commerce
product photo — so its output legitimately differs from its reference (the raw
input snapshot) in background, lighting, crop, and often the piece's pose. The
synthetic-angle judge counts exactly those differences as evidence of a
different piece, and until this date both call sites shared it: live sub-job
`6b3eda1e` scored **0.0** on a flawless output. Background operations now use
`SUBJECT_PRESERVATION_JUDGE_PROMPT`, which names those differences as intended
and scores piece identity alone — see `docs/ai-integration.md`'s Call Site 2
and `app/providers/gemini_qa.py`. The `0.92` threshold is unchanged; the new
prompt's anchors are shaped to keep it meaningful. Sub-jobs flagged by the
old judge are cleared with `POST /api/v2/internal/qa/rescore-flagged-background`
(ops scope), which re-runs the judge without spending the client's
`attempt_count` budget on a backend defect.

**Retry.** `POST /jobs/{job_id}/retry` — see §5. Reuses `retry_service.execute_retry`
and `job_service.check_retry_preconditions` unmodified; `409 ANGLE_JOB_RETRY_NOT_ALLOWED`
on an `ANGLE_GENERATION` job.

---

## 14. MATCH (Phase 18)

`MATCH` generates 1-4 companion-piece "variants" from one uploaded style-reference
photo — `POST /api/v2/match`. See `phases/phase-18-match.md` and
`docs/ai-integration.md`'s Mode D. Unlike background operations (always exactly one
sub-job) and like angle generation, MATCH genuinely fans out — but its sub-jobs are
indexed by `variant_index` (0-based), not `angle`, and every one has `angle IS NULL`.

**Operation matrix** — goes through the same `GeminiProvider` seam every other
operation uses, unmodified:

| Operation | Input | Prompt source | Output |
| :--- | :--- | :--- | :--- |
| `MATCH` | One uploaded photo (style reference) + `target_category` | `config.global.operations.MATCH.prompt`, with `{target_category}` substituted at call time (`job_service.resolve_match_prompt`) | A standalone studio photo of a **different** item matching the reference's metal/stone/design language |

- **Category rules (§1) apply, unchanged.** `target_category` is validated against
  `payload.categories`, the same single source of truth `/generate`'s `category_code`
  uses — see §1's MATCH note. `job.category_code` is where the resolved
  `target_category` is stored (MATCH reuses this existing column; it is not `NULL`
  for MATCH the way it is for background operations).
- `requested_angles` is reused as `variant_count` (1-4) for a MATCH job — the third
  reinterpretation of that column name (angle count → always-1 for background → variant
  count for MATCH). See `docs/schema.md`'s `jobs.requested_angles` note.
- `operations.MATCH.enabled` gates `POST /match` — `422 OPERATION_DISABLED` if `false`
  or absent, same code every other operation uses for the same condition.
- `unit_cost_usd` resolves per-operation
  (`config.global.operations.MATCH.unit_cost_usd`), falling back to
  `config.global.unit_cost_usd` when unset — same generic fallback rule §10 states for
  every other operation, nothing MATCH-specific.
- **No QA gate.** See §7's MATCH note — a deliberate scope decision, not a gap.

**Retry — the one genuinely new job-level business rule this phase introduces, on a
route shared with background operations.** Before Phase 18, `POST /jobs/{job_id}/retry`
always retried `sub_jobs[0]` — correct only because every job-level-retryable operation
that existed (background operations) has exactly one sub-job. MATCH has 1-4, so the
route was generalized:

- It now retries **every `FAILED` sub-job on the job**, not just one.
- It's **all-or-nothing**: every failed sub-job's own retry preconditions (attempt
  ceiling, expired input) are validated *before* any of them is executed. A job with
  two failed variants where only one still has an unexpired input returns `409` for
  the whole request rather than silently retrying the one that's still eligible and
  skipping the other — this project's "fail loud, don't silently degrade" posture,
  not a new one invented for this phase.
- The idempotency target changed from being keyed on a specific sub-job's ID to
  `f"{job.id}:retry"` — a fixed function of the job alone. This was necessary because
  MATCH's retryable set varies request-to-request (which variants are `FAILED` at
  retry time isn't fixed the way "the job's one sub-job" was), and a replay must
  always compare against the same stored target regardless of what's `FAILED` *now*.
- **Verified as a strict behavioral no-op for the existing background-operation
  case**: the idempotency target string is never observable by a client (compared
  only server-side in Redis), no existing test asserted on its literal value, and a
  background-job with exactly one `FAILED` sub-job still retries exactly that one
  sub-job under the all-or-nothing logic above (a set of size 1 is trivially
  all-or-nothing). A regression test proving background-retry-replay is still a
  no-op was added alongside this change.
- On success, dispatches `match.process` per retried variant (or `background.process`
  for a background job, dispatch chosen by `job.operation`) — same "reset to
  `PENDING`, record `RETRY_REQUESTED`, dispatch fresh" shape §5 describes for the
  single-sub-job case, just looped.

See `app/api/v2/retry.py::retry_job` for the implementation and
`docs/api-routes.md`'s "Status and retry" note under Companion-Piece Generation for
the client-facing contract.

---

## 15. RECOLOR (Phase 19)

`RECOLOR` recolors a masked gemstone region to a palette color from one uploaded
source photo plus one uploaded mask — `POST /api/v2/recolor`. See
`phases/phase-19-recolor.md` and `docs/ai-integration.md`'s Mode E. Like background
operations (always exactly one sub-job) and unlike MATCH, RECOLOR does not fan out.

**Operation matrix** — goes through the same `GeminiProvider` seam every other
operation uses, unmodified. The mask never travels to Gemini as a mask — the Gemini
API has no mask parameter (confirmed from `app/providers/gemini.py::_call_api`,
which takes only image + text):

| Operation | Input | Prompt source | Output |
| :--- | :--- | :--- | :--- |
| `RECOLOR` | One uploaded photo + one uploaded mask + `palette_code` | `config.global.operations.RECOLOR.prompt`, with `{palette_prompt}` substituted at call time (`job_service.resolve_recolor_prompt`) | The original source photo, unchanged outside the mask, with the masked gemstone recolored |

- **No category rules (§1) apply** — a RECOLOR job has `category_code: NULL`, same
  as a background job, not MATCH.
- `requested_angles` is always `1` for a RECOLOR job — see §3's note. `PARTIAL_SUCCESS`
  is structurally unreachable, same reasoning §13 gives for background operations.
- `operations.RECOLOR.enabled` gates `POST /recolor` — `422 OPERATION_DISABLED` if
  `false` or absent, same code every other operation uses.
- `palette_code` must name an **active** entry in `config.global.palette` —
  `422 PALETTE_NOT_FOUND` / `422 PALETTE_INACTIVE` otherwise, same shape preset
  validation (§13) already uses.
- **Mask contract**, validated on ingest, before any provider call — `422
  VALIDATION_ERROR` naming the specific violation, never a silent best-effort:

  | Rule | Failure detail |
  | :--- | :--- |
  | PNG format | names the format actually received |
  | Single-channel 8-bit grayscale, no alpha | names the mode actually received |
  | Dimensions exactly match the source image | names both the mask's and the source's dimensions |
  | Binary values only (0 and 255) | names the count of intermediate values found |
  | At least `MASK_MIN_COVERAGE_PCT` (default 0.5%) white | names the measured coverage |
  | At most `MASK_MAX_COVERAGE_PCT` (default 60.0%) white | names the measured coverage |

  See `app/services/mask_validation.py`.
- `unit_cost_usd` resolves per-operation (`config.global.operations.RECOLOR.unit_cost_usd`),
  falling back to `config.global.unit_cost_usd` when unset — same generic fallback
  rule §10 states for every other operation.
- **No QA gate.** See §7's RECOLOR note — a deliberate scope decision, verified by a
  deterministic compositing test instead.

**Mask-to-Gemini conveyance and generate-then-composite.** Since Gemini has no mask
parameter, the mask does its work in two places:

1. **Before the call:** the mask is eroded by `MASK_ERODE_PX` (default 2px — pulls
   the edit region back off metal/prongs a hand-drawn mask routinely catches at its
   boundary), then burned into a solid magenta overlay composited onto the source.
   This single overlay image is what's sent to Gemini, alongside a prompt
   referencing "the region marked in magenta."
2. **After the call:** the mask is feathered by `MASK_FEATHER_PX` (default 3px, a
   *separate* pass from erosion — feathering softens the compositing alpha, erosion
   shrinks the edit region; they are not the same operation applied twice) and used
   to composite Gemini's raw output back onto the **original, full** source image.
   Everywhere the feathered mask is 0, the stored `OUTPUT` asset's pixel is the
   original source's pixel, exactly. This is the **only** operation in this
   codebase where the client-facing artifact is not the provider's raw response.

See `app/services/recolor_service.py` for the implementation.
**Unvalidated against a real model call** — no real `GEMINI_API_KEY` exists in this
environment, same gap every phase since 6 has hit. If the magenta-overlay approach
doesn't hold up against real jewelry macro photography, that's a correction to
`docs/ai-integration.md`'s Mode E and this rule, not a silent code change.

**Post-Phase-20 incident (2026-08-24):** a real RECOLOR request on the live
Render free-tier deployment pushed memory from a ~200MB baseline to 487MB
(91% of the 512MB limit) in one sample, OOM-killing the container — root
cause was `_build_overlay` decoding the client's full-resolution upload
with no size cap (a real jewelry photo can be 4000px+ on the long edge).
Fixed by capping the overlay's *working* resolution at
`settings.WORKING_MAX_EDGE` (env var, default 2048px) before
erosion/compositing — see `app/config.py`'s own note. This
does not weaken the guarantee above: `_composite_result` still decodes the
*original* `source_bytes`/`mask_bytes` at full resolution for the final
output, so "byte-identical outside the mask" remains a full-resolution
claim. Only the throwaway overlay sent to Gemini got smaller.

**Retry.** `POST /jobs/{job_id}/retry` — see §5. A RECOLOR job always has exactly
one sub-job, so Phase 18's all-or-nothing multi-sub-job generalization applies here
as the trivial "set of size 1" case, same as it already does for background
operations — no further changes to the retry route were needed for RECOLOR, only a
third entry in its dispatch-task lookup (`app/api/v2/retry.py`).

---

## 16. MIX (Phase 20)

`MIX` merges a masked region from one uploaded piece into a masked region of another
— `POST /api/v2/mix`. See `phases/phase-20-mix.md` and `docs/ai-integration.md`'s
Mode F. Like RECOLOR and background operations (always exactly one sub-job) and
unlike MATCH, MIX does not fan out.

**Operation matrix** — goes through the same `GeminiProvider` seam every other
operation uses, unmodified. Like RECOLOR, the mask never travels to Gemini as a
mask — the Gemini API has no mask parameter:

| Operation | Input | Prompt source | Output |
| :--- | :--- | :--- | :--- |
| `MIX` | Two uploaded photos ("primary" — the piece receiving the graft, "secondary" — the piece grafted from) + one mask on each | `config.global.operations.MIX.prompt` — a complete, final string, no runtime template placeholder (unlike MATCH/RECOLOR) | The primary photo's frame, with the secondary photo's masked region deterministically grafted in and its seam refined |

- **No category rules (§1) apply** — a MIX job has `category_code: NULL`, same as
  RECOLOR and background operations, not MATCH.
- `requested_angles` is always `1` for a MIX job — see §3's note. `PARTIAL_SUCCESS`
  is structurally unreachable, same reasoning §13 gives for background operations.
- `operations.MIX.enabled` gates `POST /mix` — `422 OPERATION_DISABLED` if `false`
  or absent, same code every other operation uses.
- **No palette, no preset** — MIX has no analogous per-request color/style choice.
- **Mask contract**, validated on ingest, before any provider call — the exact same
  six rules RECOLOR's mask contract table lists (§15), applied **twice, completely
  independently**: once for the primary photo/mask pair, once for the secondary
  pair. Each mask is checked against its *own* source's dimensions, not the other
  pair's. See `app/services/mask_validation.py` — unmodified, called twice.
- **No cross-mask validation at ingest time** (e.g. a minimum region size relative
  to `MIX_SEAM_BAND_PX`/`MASK_FEATHER_PX`, or a bound on the primary/secondary
  region aspect-ratio mismatch) — deliberately deferred, same "flag the risk, don't
  solve it speculatively" posture RECOLOR's own reality check gave prong-bleed
  calibration. See the seam-band note below for the specific, real limitation this
  leaves open.
- `unit_cost_usd` resolves per-operation (`config.global.operations.MIX.unit_cost_usd`),
  falling back to `config.global.unit_cost_usd` when unset — same generic fallback
  rule §10 states for every other operation.
- **No QA gate.** See §7's MIX note — a fourth distinct reason, not a fourth
  posture, verified by a deterministic compositing test instead.

**Deterministic rough-composite, then seam-scoped generate-then-composite.** Unlike
every other operation, MIX's first real step is not a provider call at all:

1. **Before any provider call:** region B is cropped to its mask's bounding box,
   scaled **preserving aspect ratio** to fit inside region A's bounding box, centred
   there, and pasted onto image A through the **intersection of both masks'
   silhouettes** — so only pixels the client actually painted on B travel, and only
   into where they painted on A. This produces a fully deterministic
   `rough_composite` — no model call, no hallucination risk on *placement*, since
   placement is Pillow math, not something asked of Gemini. **Both of those
   properties are 2026-08-28 corrections to real defects; see the defect note at the
   end of this section.**
2. **The seam-band mask:** a ring `MIX_SEAM_BAND_PX` pixels wide (default 6)
   straddling the **graft's** boundary — `dilate(g, band_px) - erode(g, band_px)`
   where `g` is the graft mask returned by the composite step. It is deliberately
   **not** built from mask A: since the graft is an intersection it can be strictly
   smaller than mask A, and banding mask A would ask the provider to blend an edge
   that does not exist while leaving the real graft edge untouched
   — marks only the visible graft edge, burned into a magenta overlay on
   `rough_composite` the same hard-edged way RECOLOR's overlay works. This is what's
   sent to Gemini, alongside a prompt asking it to blend only the marked seam.
3. **After the call:** the seam-band mask is feathered by `MASK_FEATHER_PX` (the
   same existing RECOLOR setting, reused rather than duplicated) and used to
   composite Gemini's raw output back onto `rough_composite` — not onto either
   original source directly. Everywhere the feathered seam-band mask is 0 — both
   the untouched rest of image A *and* the already-correct interior of the graft —
   the stored `OUTPUT` asset's pixel is `rough_composite`'s own pixel, exactly.

**Known limitation, not fixed in this phase:** for a masked region narrower than
roughly `2 * (MIX_SEAM_BAND_PX + MASK_FEATHER_PX)` in either dimension, the
post-call Gaussian feather can bleed through the graft's interior from both sides of
the seam-band ring at once, letting the provider's output reach pixels this rule
otherwise guarantees are protected. No ingest-time minimum-region-size check exists
yet — see `app/services/mix_service.py::_seam_band_mask`'s own docstring. Found and
documented while building this phase's own central pixel-identity test, not a
theoretical concern.

See `app/services/mix_service.py` for the implementation.
**Unvalidated against a real model call** — no real `GEMINI_API_KEY` exists in this
environment, same gap every phase since 6 has hit. Both the non-aspect-preserving
scale-to-fit and the seam-only refinement strategy are unvalidated against real
jewelry macro photography; a correction belongs in `docs/ai-integration.md`'s
Mode F and this rule, not silently in code.

**Post-Phase-20 incident (2026-08-24):** same live Render OOM the RECOLOR §15
note above describes, root-caused there to RECOLOR's own overlay-building step.
MIX's `_build_seam_overlay` got the identical fix — downscaled to
`settings.WORKING_MAX_EDGE` before the magenta seam-band fill, since it's also
a throwaway input to Gemini. **`_build_rough_composite`'s *output* still does
not downscale** — it's the base both the seam overlay is built from and the
final compositing step composites back onto, so it must stay at source A's
real, full resolution the same way RECOLOR's own final compositing step does.
At the time, this left `_build_rough_composite` decoding **four**
full-resolution images at once (source A, mask A, source B, mask B), flagged
as MIX's own remaining memory hotspot.

**2026-08-25 follow-up, same function:** source B and mask B are now
downscaled to `settings.WORKING_MAX_EDGE` *before* the crop — correctness-
neutral, since both were always going to be cropped-then-resized into region
A's bounding box regardless (already a lossy, non-aspect-preserving
scale-to-fit). Source A and mask A are untouched, still full resolution, per
the paragraph above. The function also no longer holds a redundant `.copy()`
of source A alongside the original — the paste now mutates source A directly,
since the original is never read again afterward. Net: two of the four
decodes are now bounded and the extra full-resolution duplicate is gone,
down from four uncapped full-resolution buffers to two. See
`app/services/mix_service.py::_build_rough_composite`'s own docstring.

**2026-08-27 — the full-resolution output guarantee was given up, deliberately.**
Neither pass above was enough. Measured against the real client upload that
broke it (3072x4096, **12.6 MP**, two photos plus two masks) the pipeline still
needed **~187 MB** of working memory against **~160 MB** of headroom on the
512 MB free instance, whose baseline was already 353 MB. Every attempt was
OOM-killed roughly three seconds in, before a single Gemini call: the live
sub-job sat at `GENERATING` with `attempt_count` still `0`, and because
Celery runs `acks_late=False` the task was dropped on the container restart
rather than retried, so the job hung at `GENERATING` indefinitely rather than
failing. **All four inputs now decode at `settings.WORKING_MAX_EDGE`, so
`rough_composite` — and therefore MIX's client-facing output — is capped at
that edge (2048px by default) instead of the primary photo's native size.**
Measured effect at 12.6 MP: peak working memory **187 MB -> 74 MB**, output
1536x2048. Decided directly with the user, who chose this over restructuring
for full resolution or moving to a larger instance.

The byte-identical-outside-the-seam-band rule above is **unchanged in
substance** — it was always stated relative to `rough_composite`, never to
either original photo, and `rough_composite` is simply a smaller image now.
What genuinely changed is output resolution, which is a real product
tradeoff and is why it was a decision rather than a silent fix.

Two consequences worth stating plainly rather than discovering later:
`MIX_SEAM_BAND_PX` remains an absolute pixel count in *working* space (the
band exists for the model to see, and that is measured in the pixels actually
sent), so the small-region limitation above now bites at a larger fraction of
the piece than it used to. And **`RECOLOR` was not changed** — §15's guarantee
is stated against the untouched original source, so `recolor_service._composite_result`
still composites at full resolution and carries the same exposure on a
12.6 MP upload. That is a known, unaddressed risk, not an oversight.

**2026-08-28 — two defects found on the first genuine client run (job `fe7d6372`),
both now fixed.** This section previously described the rough-composite as cropping
B to its mask's *bounding box* and stretching it to *exactly fill* A's box, with the
aspect distortion flagged as "a deliberate simplification, not yet validated against
real client pieces." That validation has now happened and both halves failed:

- **Mask B's shape was discarded.** It was used only to compute a bounding box; the
  raw rectangle was grafted. On the real job, mask B was two curved bands on
  opposite sides of the piece, so their shared bounding box was only **39.5%
  painted** — **60% of the grafted content was unpainted mannequin and background**,
  which landed in the middle of the client's pendant as a flat beige blob. Mask B is
  now a paste alpha in its own right.
- **The stretch-to-fill distorted badly.** Two long thin bands squashed into a
  compact pendant silhouette cannot look right at any masking quality. The scale is
  now `min(w_ratio, h_ratio)` with the result centred.

The seam band consequently moved from mask A to the graft mask (see step 2 above) —
once the graft is an intersection, mask A is no longer its boundary. Regression
tests: `test_graft_excludes_unpainted_parts_of_mask_b_bbox`,
`test_graft_preserves_aspect_ratio_of_region_b`.

**Still open, and worth stating plainly:** MIX places content by fitting B's masked
region into A's masked region. When the two shapes differ a lot the result is a
scaled silhouette swap, not a semantic transplant — the operation works best when
the two masked regions are roughly comparable in shape. There is still no
ingest-time check for that (see the cross-mask validation note above).

**Retry.** `POST /jobs/{job_id}/retry` — see §5. A MIX job always has exactly one
sub-job, so Phase 18's all-or-nothing multi-sub-job generalization applies here as
the trivial "set of size 1" case, same as it already does for RECOLOR and
background operations — no further changes to the retry route were needed for MIX,
only a fourth entry in its dispatch-task lookup (`app/api/v2/retry.py`).
