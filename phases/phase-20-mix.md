# Phase 20 — MIX (Two-Piece Masked Merge)

## Reality check before writing this

**State of the codebase as of 2026-08-16, after Phase 19 merged (PR #25):** five
operations live — `ANGLE_GENERATION`, `BACKGROUND_REMOVAL`, `BACKGROUND_REPLACEMENT`,
`MATCH`, `RECOLOR` — all reusing one job/sub-job/asset state machine, one
`GenerationProvider` seam (`app/providers/gemini.py`, real SDK never exercised in
tests — no `GEMINI_API_KEY` exists in this environment, same gap every phase since 6
has hit), one rate limiter, one cost ledger, one retry route. `RECOLOR`
(`app/services/recolor_service.py`) proved the whole mask-conveyance and
generate-then-composite pattern this phase extends: mask contract validation on
ingest (`app/services/mask_validation.py::validate_mask`), erosion before the
provider call (`_erode`, `MASK_ERODE_PX`), feathering after it (`_feather`,
`MASK_FEATHER_PX`), a magenta colour-overlay conveyance strategy since Gemini has no
mask parameter (confirmed from `app/providers/gemini.py::_call_api` — only
`Part.from_text`/`Part.from_bytes` exist), and a compositing step
(`_composite_result`) that makes the client-facing `OUTPUT` asset something other
than the provider's raw response. MIX is the first operation that needs **two**
independent source images and **two** independent masks, and the first that needs a
deterministic image-assembly step **before** any provider call at all — every prior
operation's first step was always "call Gemini."

**Why this is the right shape, not a bigger RECOLOR call:** cross-image spatial
reasoning — "take the gemstone from photo B and place it correctly, at the right
scale and position, into photo A" — is the weakest capability in play for any
current-generation image model, confirmed by nothing in this codebase's own testing
(no real `GEMINI_API_KEY` exists to test it) but consistent with why `phases/phase-19-
recolor.md`'s own closing note flagged this as MIX's real risk before RECOLOR was even
built: a single "merge these two masked regions" prompt asks the model to do
placement, scale, and blend all at once, with no ground truth for where region B's
content should land in region A's frame. Splitting that into a **deterministic
rough-composite step** (crop, scale, align, paste — plain Pillow, no model call,
therefore no hallucination risk on placement) **followed by a refinement call scoped
only to the visible seam** turns "get the whole graft right" into "make an already-
correctly-placed graft's edge look natural," a narrower, better-suited task for an
image model. This is the same reasoning Phase 18 used before Phase 19 (prove fan-out
on the smallest surface first) and Phase 19 used before this phase (prove one mask,
one source, one output first) — build on the smallest necessary extension of what's
already proven, not the largest.

**Depends on Phase 19, complete and merged.** Nothing here touches `variant_index` or
`match_service.py`. What it reuses directly, unmodified: `mask_validation.validate_mask`
(called twice per request — once per source/mask pair, no changes to its contract or
signature), the erosion/feathering primitives' *shape* (`recolor_service._erode`/
`_feather`, reimplemented as this phase's own module-level functions rather than
imported, following the exact "two independent constants/helpers that happen to share
a shape" precedent `recolor_service.py`'s own docstring already establishes relative
to `background_service.py`/`match_service.py` — no service module imports another
service module's private helpers anywhere in this codebase), `orchestration.dispatch_job`
is **not** used (MIX has no fan-out, same as RECOLOR — dispatched directly, one
sub-job), `status_rollup.compute_parent_status`, the generalized
`POST /jobs/{job_id}/retry` (MIX's job always has exactly one sub-job, the same
trivial "set of size 1" case Phase 18 proved is a no-op), `cost_service.record_cost_event`,
`config_versions_repo`, `storage_service`, `image_validation.inspect_and_validate`.

**What's genuinely new, in order of how much this codebase has never done it before:**

1. **A second source image and a second mask on one sub-job.** Every prior operation,
   including `RECOLOR`, has exactly one `input_asset_id` and (for RECOLOR) one
   `mask_asset_id`. MIX needs two of each, on the *same* sub-job row — not two
   sub-jobs, since the output is one merged image, not two independent results.
2. **A deterministic pre-provider assembly step.** Every prior operation's first
   real step is a provider call (Mode A-D) or a provider-call-with-a-pre-built-overlay
   (RECOLOR). MIX's first real step is Pillow-only image assembly — crop region B via
   its mask's bounding box, scale it to fit region A's bounding box, paste it onto
   source A. No model call is involved in *placement* at all; only the *seam* is
   Gemini's job.
3. **A seam-band mask, not a filled-region mask.** RECOLOR's compositing mask marks
   "the region to change." MIX's post-composite mask marks a **ring** around the
   graft boundary — the visible seam, not the graft's interior (which is already
   correct, deterministically, from the rough-composite step) and not the untouched
   rest of image A. This is a new shape of mask this codebase has never built, even
   though it reuses the same erode/feather/magenta-overlay/generate-then-composite
   machinery RECOLOR proved.
4. **A four-slot presign response and a four-field request** — `RECOLOR` needed two
   upload slots (source + mask), always both. MIX needs four (two sources, two masks),
   always all four — the same "no extra request flag, the operation structurally
   requires every slot" reasoning RECOLOR already established for its two, extended.
5. **No new palette or per-operation color config** — unlike RECOLOR, MIX has no
   `palette_code` and no analogous new `config.global` substructure. Its only new
   config surface is one `operations.MIX` entry, the smallest addition of any v3
   phase so far.

---

## Step 1 — Schema

### What to do

New migration `0017_add_mix_operation.py`. Same "one migration, not two" reasoning
`0015`'s own docstring established and `0013`'s before it: the new enum value is never
compared against or inserted as data within this same migration's transaction, so
`ALTER TYPE ... ADD VALUE` and the column DDL are safe together on Postgres 15.

1. `ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'MIX'`.
2. Add `sub_jobs.secondary_input_asset_id UUID NULL FK -> assets.id` — the second
   source photo (image B). **Reuses `AssetKind.INPUT`**, not a new asset kind — it's
   an ordinary uploaded photograph, structurally identical to `input_asset_id`'s
   asset, distinguished only by which FK column points at it. This mirrors exactly
   how `sub_jobs.background_asset_id` (migration 0011) reuses `AssetKind.INPUT` for
   a second `BACKGROUND_REPLACEMENT` reference photo rather than inventing a new kind
   — same precedent, not a new one.
3. Add `sub_jobs.secondary_mask_asset_id UUID NULL FK -> assets.id` — the second
   mask (region B on image B). **Reuses `AssetKind.MASK`** (added by migration 0015),
   not a new kind — it's validated by the exact same `mask_validation.validate_mask`
   contract RECOLOR's mask already is, just against a different source image's
   dimensions. No new `asset_kind_t` value needed for this phase at all — the first
   v3 phase to add zero new enum values to `asset_kind_t`.
4. **Do not touch `ux_sub_jobs_job_single` or add a MIX-specific partial index.**
   A `MIX` job always has exactly one sub-job (same shape as `RECOLOR` and background
   operations, not `MATCH`'s 1-4) — `angle IS NULL AND variant_index IS NULL` already
   covers it, exactly the same "no schema conflict exists here" reasoning `0015`'s own
   Step 1 stated explicitly for RECOLOR. State this in the migration docstring again,
   for the same reason `0015` did: a future reader should not assume every new
   operation needs an index change just because `MATCH` did.
5. Update `app/db/models/enums.py::Operation` and the SQLAlchemy `SubJob` model
   (`secondary_input_asset_id: Mapped[uuid.UUID | None]`,
   `secondary_mask_asset_id: Mapped[uuid.UUID | None]`).
6. `jobs.requested_angles` is `1` for a MIX job — same posture as RECOLOR and
   background operations (one sub-job, one merged output), not MATCH's variant count.
   Add MIX as the reinterpretation column's fifth case in `docs/schema.md`.

### Checkpoint 1

- [ ] `alembic upgrade head` succeeds against a fresh `testcontainers` Postgres and is
      then confirmed live against the real Supabase database (`rsolykmjupiusdujajgj`)
      via `mcp__supabase__execute_sql` — `select unnest(enum_range(null::operation_t))`
      includes `MIX`
- [ ] `alembic downgrade -1` succeeds, leaves `MIX` in place (no `DROP VALUE`, same
      documented posture every prior enum-adding migration has established), drops only
      the two new columns
- [ ] A test creates one `sub_jobs` row with `input_asset_id`, `mask_asset_id`,
      `secondary_input_asset_id`, and `secondary_mask_asset_id` all set, `angle` and
      `variant_index` both `NULL`, under a `MIX` job, and confirms
      `ux_sub_jobs_job_single` still allows it and still rejects a second angle-less,
      variant-less sub-job under the same job
- [ ] SQLAlchemy model and live schema match exactly — autogenerate diff produces no
      changes
- [ ] `docs/schema.md`'s `sub_jobs` section gains both new columns; `operation_t`'s
      enum list gains `MIX`; the `jobs.requested_angles` reinterpretation note gains MIX
      as its fifth case; **explicitly note that `asset_kind_t` gained no new value this
      phase**, unlike every prior v3 phase, so a future reader doesn't go looking for one

---

## Step 2 — Config

### What to do

Following `0014`'s/`0016`'s exact merge-not-replace pattern (new `config_versions`
row, `global.operations` extended without disturbing `BACKGROUND_REMOVAL`/
`BACKGROUND_REPLACEMENT`/`MATCH`/`RECOLOR`, Redis `config:active` cache invalidated,
CLAUDE.md Hard Rule 11):

```python
NEW_OPERATIONS = {
    "MIX": {
        "enabled": True,
        "prompt": (
            "This image shows two jewelry pieces that have already been merged: "
            "one piece's element has been placed into the other. Blend only the "
            "seam marked in solid magenta so the graft looks like a single, "
            "naturally manufactured piece — smooth the transition in metal, "
            "lighting, and shadow exactly at that boundary. Do not alter "
            "anything else in the image, and do not move, resize, or reshape "
            "either piece."
        ),
        "unit_cost_usd": 0.02,  # placeholder, same uncalibrated status as every other seeded cost
    }
}
```

**No new top-level `global` substructure** — unlike RECOLOR's `global.palette`, MIX
introduces no analogous new list. This is the smallest config addition of any v3
phase: one `operations.MIX` entry, nothing else. Say so explicitly in the migration's
docstring, the same "confirm what didn't change, not just what did" discipline `0015`
used for `ux_sub_jobs_job_single`.

### Checkpoint 2

- [ ] Migration `0018_add_mix_config.py` inserts a new `config_versions` row with
      `MIX` merged into `payload.global.operations` (not replacing the four existing
      keys — same defensive check `0014`/`0016` already had to get right, this is the
      *fourth* migration to touch `operations`); deactivates the prior version;
      invalidates the Redis `config:active` cache
- [ ] `find_operation_config(config_version, Operation.MIX)` returns the seeded dict
- [ ] `GET /api/v2/config`'s response is unaffected beyond the operation being present
      internally — MIX has no client-visible config surface beyond
      `operations.MIX.enabled` gating the route (no palette, no preset list), so there
      is nothing new for `docs/api-routes.md`'s Config section to document

---

## Step 3 — Ingest validation and presign

### What to do

**Request/route shape**, new `app/api/v2/schemas/mix.py::MixRequest`:

```python
class MixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_storage_path: str          # image A — the piece receiving the graft
    primary_mask_storage_path: str     # region on A to receive the graft
    secondary_storage_path: str        # image B — the piece being grafted from
    secondary_mask_storage_path: str   # region on B to cut
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

`primary`/`secondary` naming, not `source`/`reference` — both images are equally real
uploaded photographs of physical pieces (unlike MATCH, where the source is explicitly
a *style reference*, not the transformed subject). "Primary" is simply the image whose
frame the final output keeps; "secondary" is where the grafted content comes from.
Name the fields this way in the schema, the route, and `docs/api-routes.md` so the
asymmetry (whose frame survives) is clear without implying either image is less real.

`POST /api/v2/uploads/presign` gains a fourth `operation` value, `"MIX"` — **four**
upload slots, all required, no extra request flag (mirrors RECOLOR's "the operation
is never valid without every slot" reasoning, extended from two slots to four):
`primary_upload`, `primary_mask_upload`, `secondary_upload`, `secondary_mask_upload`,
same `upload_url`/`storage_path`/expiry shape as every existing slot.

New `POST /api/v2/mix` route, `app/api/v2/mix.py`, same shape as `recolor.py`.

**Validation, in order — all `4xx` before any job row is created:**

1. `operations.MIX.enabled` (`422 OPERATION_DISABLED`, same code every operation uses)
2. `primary_storage_path` exists in `jewelry-inputs`, belongs to this client, passes
   `image_validation.inspect_and_validate` (`422 ASSET_NOT_FOUND` / `ASSET_NOT_OWNED`
   / `VALIDATION_ERROR`, all existing codes)
3. `primary_mask_storage_path` exists, belongs to this client, passes
   `mask_validation.validate_mask` against the **primary** source's dimensions
4. `secondary_storage_path` exists, belongs to this client, passes
   `image_validation.inspect_and_validate` — independent of step 2; the two source
   images are not required to share dimensions, aspect ratio, or category
5. `secondary_mask_storage_path` exists, belongs to this client, passes
   `mask_validation.validate_mask` against the **secondary** source's dimensions —
   `mask_validation.validate_mask` is called twice in this route, once per pair,
   completely independently; it has no knowledge of MIX or of a second image, and
   needs no change to serve this

**No cross-mask validation at ingest time** (e.g. "region B must be smaller than
region A," "aspect ratios must roughly match") — deliberately deferred to Step 4's
deterministic scale-to-fit step, which handles any relative size by construction
(it always scales region B's crop to exactly fit region A's bounding box, whatever
that ratio is). Flagged as a real, unvalidated risk if that ratio is extreme (e.g.
grafting a whole necklace clasp into a single small gemstone's region) — same
"flagged, not solved" posture RECOLOR's own reality check gave prong-bleed
calibration. Do not add speculative ratio limits without a real case showing they're
needed.

### Checkpoint 3

- [ ] `POST /api/v2/uploads/presign` with `{"operation": "MIX"}` returns all four
      slots: `primary_upload`, `primary_mask_upload`, `secondary_upload`,
      `secondary_mask_upload`
- [ ] `POST /api/v2/mix` with all four valid assets creates one job
      (`operation: MIX`, `requested_angles: 1`, `category_code: NULL`) and one sub-job
      (`angle: NULL`, `variant_index: NULL`, all four asset FKs set)
- [ ] Each of the four validation steps has a dedicated failing test (bad/missing
      primary source, bad/missing primary mask, bad/missing secondary source,
      bad/missing secondary mask), each asserting the specific violation named, not a
      generic message — same specificity discipline every prior mask/image validation
      test in this codebase already follows
- [ ] A mask whose dimensions don't match *its own* source (primary mask vs. primary
      source's dimensions, independently for secondary) returns `422` naming both —
      confirms `mask_validation.validate_mask`'s existing per-pair behavior is
      unaffected by being called twice in one request
- [ ] Idempotency: replaying `(client_id, Idempotency-Key)` with an identical body
      returns the original `job_id`, `200`; a different body under the same key
      returns `409` — same test shape every other job-creating route already has
- [ ] `docs/api-routes.md` gets a new `## Two-Piece Masked Merge (Phase 20)` section
      and the Uploads section documents the `MIX` → four-slot presign shape

---

## Step 4 — Rough-composite, seam overlay, and worker

### What to do

New `app/services/mix_service.py`, mirroring `recolor_service.py`'s
rate-limit→provider-call→cost-event→success/fail→recompute shape, plus a genuinely
new step **before** the rate-limit loop even starts:

**Before the call — deterministic rough-composite, no provider involved, never sent
to the client on its own:**

1. Download source A, mask A, source B, mask B bytes.
2. Compute mask B's bounding box (the smallest rectangle containing its white
   region) and crop source B to it, keeping mask B (cropped to the same box) as
   an alpha channel — `Image.composite`/`putalpha` over the crop, so only the
   masked pixels of B survive, not B's whole rectangular crop.
3. Compute mask A's bounding box on source A. Scale the cropped-and-masked region B
   from step 2 to **exactly fit** mask A's bounding box dimensions (`Image.resize`,
   aspect ratio not preserved — a deliberate simplification, not an oversight; see
   Step 3's note on why no ingest-time ratio limit exists yet).
4. Paste the scaled region onto a copy of source A at mask A's bounding box,
   using mask A itself (also scaled to that exact box, since a bounding box is not
   necessarily the same shape as the mask's actual silhouette within it) as the paste
   alpha, so pixels inside the box but outside mask A's actual silhouette are left as
   source A's original pixels, not overwritten by the crop's rectangular corners.
   This produces `rough_composite` — fully deterministic, reproducible from A/B/mask
   A/mask B alone, no randomness, no model call.
5. Build a **seam-band mask**: a ring around mask A's boundary, `MIX_SEAM_BAND_PX`
   pixels wide (new setting, default `6`) — `dilate(mask_a, band_px)` minus
   `erode(mask_a, band_px)` (dilation via `ImageFilter.MaxFilter`, mirroring
   `recolor_service._erode`'s existing `MinFilter` use for the opposite operation;
   no new imaging dependency). This marks *only* the visible graft edge — not the
   graft's interior (already correct by construction from step 4) and not the
   untouched rest of image A.
6. Composite a solid magenta fill over `rough_composite` through the seam-band mask
   (hard edge, same reasoning `recolor_service._build_overlay` already established
   for why the overlay sent to Gemini needs a hard boundary, not a feathered one).
   This single image — `rough_composite` with a magenta seam ring — is what's sent
   to Gemini as the sole reference image.

**The call itself:** `provider.generate(prompt, [overlay_bytes], seed)` — same
`GeminiProvider`, same rate limiter, same cost-event-before-evaluation posture every
prior operation establishes. No provider code changes.

**After a successful call — generate-then-composite, scoped to the seam only:**

7. Feather the seam-band mask separately by `MASK_FEATHER_PX` (the same existing
   setting RECOLOR already uses for its own post-call composite — no new constant
   needed here, since the purpose — softening the alpha used for pixel blending after
   the call — is identical in kind, just applied to a ring instead of a filled
   region).
8. Composite the provider's raw output back onto `rough_composite` (**not** onto
   either original source directly — `rough_composite` is already the correct
   graft-placed image; only its seam needs Gemini's touch) using the feathered
   seam-band mask as the alpha. Everywhere that mask is `0` — which includes both the
   untouched rest of image A *and* the already-correct interior of the graft — the
   output pixel must be `rough_composite`'s pixel, exactly.
9. Store this final composited image as the `OUTPUT` asset. Same "the provider's raw
   response is not the client-facing artifact" exception RECOLOR introduced,
   narrower here: only the *seam ring*, not the whole non-background region, is
   provably bounded to the model's influence.

**No QA gate — a fourth distinct reason, not a fourth posture.** There remain exactly
two postures (`docs/business-rules.md` §7): "no gate at all" (real-photo angles,
MATCH, RECOLOR) and "always gated" (`SYNTHETIC` angles, background operations). MIX
joins the first posture, for its own reason, distinct from the other three already
distinguished there: MATCH's output is *supposed* to differ from its source
(wrong tool). RECOLOR's output is provably identical to *the original untouched
source* outside a static mask (pixel-exact, verified deterministically). **MIX's
output is provably identical to `rough_composite` — itself already a deterministic
merge of two different pieces' photos, not an untouched original — outside the seam
band.** The correctness claim is real and testable the same way RECOLOR's is, but the
baseline it's compared against is a synthesized intermediate, not either original
photo. Write this as the fourth entry in §7's list, explicitly distinguished from
RECOLOR's, not folded into it.

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`,
`seed` — same fields, same reason, every prior operation already establishes.

### Checkpoint 4

- [ ] A real `POST /api/v2/mix` against `testcontainers` Postgres + real local Redis +
      real Supabase Storage runs to `COMPLETED`, fixture-driven Gemini (no real
      `GEMINI_API_KEY` exists in this environment, same posture every prior phase's
      verification has used)
- [ ] **Rough-composite correctness, asserted programmatically on fixtures, before any
      provider call is even involved:** given a fixture source A, fixture mask A
      (known bounding box), fixture source B, fixture mask B (known bounding box), the
      deterministic `rough_composite` step places cropped-and-masked region B at
      exactly mask A's bounding box on source A, scaled to fit, and leaves every pixel
      of source A outside mask A's bounding box byte-identical to source A's own
      pixels. This is Step 4's `_build_overlay` equivalent — the single most important
      test of this checkpoint, since everything downstream depends on the graft being
      placed correctly *before* Gemini ever sees it
- [ ] **Off-seam pixel identity, asserted programmatically, not visually:** given the
      same fixtures plus a fixture Gemini response image (a deterministic fixture
      simulating "the model changed everything, not just the seam"), the final
      composited `OUTPUT` asset's pixels **outside the seam-band mask's white region**
      are byte-identical to `rough_composite`'s own pixels at every one of those
      coordinates — both inside the untouched rest of image A and inside the already-
      placed graft's interior. This is this phase's equivalent of RECOLOR's own
      single most important test, extended to a two-region baseline
- [ ] A test with fixture masks A and B of clearly different aspect ratios confirms
      the scale-to-fit step does not crash and produces the documented (deliberately
      non-aspect-preserving) stretch, not a silent crop or a raised error — pin the
      current behavior with a test even though it's flagged as an open risk, the same
      "test the code as written, flag the calibration gap separately" split RECOLOR's
      own erosion test made for prong-bleed
- [ ] Real seam-blend quality (does the refinement call actually make a graft look
      manufactured, not obviously composited, on **real** jewelry photography) is
      **not achievable in this environment** — no real `GEMINI_API_KEY` or real
      client pieces exist to test against, same category of gap as RECOLOR's
      uncalibrated prong-bleed erosion and every other uncalibrated open decision.
      Flag this explicitly as a new open decision; do not claim it's solved because
      the seam-band *code* is tested
- [ ] A `cost_events` row is written per provider call attempt, `operation: "mix"`,
      same as every other operation
- [ ] `POST /api/v2/jobs/{job_id}/retry` works unchanged against a `FAILED` MIX job —
      add one regression test confirming the trivial single-sub-job case, rather than
      assuming it from RECOLOR's or background operations' own regression tests

---

## Step 5 — Retention and docs

### What to do

- **Retention:** no change needed to `docs/business-rules.md` §11 or
  `app/services/retention_policy.py::compute_expires_at` — `secondary_input_asset_id`
  points at an `AssetKind.INPUT` asset (90 days, existing rule) and
  `secondary_mask_asset_id` points at an `AssetKind.MASK` asset (7 days, existing
  rule from Phase 19). State this explicitly in the self-audit rather than silently
  relying on it — confirm, don't assume, the same discipline `0015`'s Step 1 used for
  `ux_sub_jobs_job_single` not needing a change.
- `docs/ai-integration.md` — add **Mode F — Two-piece masked merge (MIX, Phase 20)**
  under Call site 1, alongside Modes A-E, following Mode E's exact structure
  (trigger/where/input/output table, then prose). State plainly that unlike Mode E,
  the pre-provider step here does real deterministic image work (crop/scale/paste),
  not just an overlay burn — the provider call refines an already-correct composite
  rather than conveying the entire edit region.
- `docs/business-rules.md` — new **§16 MIX (Phase 20)**, modeled on §15's structure
  (operation matrix table, bullet list of rule deltas, the fourth distinct no-QA-gate
  reason cross-referenced from §7). Extend §1's category carve-out line with MIX
  alongside RECOLOR and background operations — no category, no angle matrix.
- `docs/api-routes.md` — new `## Two-Piece Masked Merge (Phase 20)` section (Step 3),
  and the Uploads section's `operation`-mode bullet list gains the `MIX` →
  four-slot case.
- `docs/schema.md` — already covered in Step 1's checkpoint.
- `phases/phase-roadmap.md` — add this phase's row, following the exact format
  Phase 19's row used. **Update the "Deferred to v3" section's note** — all three
  originally-named v3 feature phases (MATCH, RECOLOR, MIX) are now built; note this
  explicitly rather than leaving the old "MIX is not [written]" sentence stale.
- `CLAUDE.md` — extend the operation-family enumeration line Phase 19 already added a
  sentence to; make it six.

### Checkpoint 5

- [ ] Every doc listed above reflects what was actually built, not this phase file's
      plan — cross-check field names, error codes, and section numbers against real
      code before writing, same self-audit discipline every prior phase file requires
- [ ] `docs/ai-integration.md`'s Mode F section explicitly distinguishes its
      pre-provider deterministic assembly step from Mode E's overlay-only approach
- [ ] `phases/phase-roadmap.md`'s "Deferred to v3" section no longer describes MIX as
      unwritten

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file.
2. Test each one for real — against `testcontainers` Postgres, real local Redis, real
   Supabase Storage, fixture-driven Gemini, same stack every prior phase has used.
   Apply and independently verify both migrations against a copy of the real live
   database, the same way Phase 19's merge was checked via `mcp__supabase__execute_sql`
   rather than trusted from the phase file's checkboxes alone.
3. Return a structured report:
   ✅ [Checkpoint] — Pass
   ⚠️ [Checkpoint] — Partial: [specific reason]
   ❌ [Checkpoint] — Fail: [specific reason]
4. Fix all failures and partials before reporting phase complete.
5. Update `docs/schema.md`, `docs/api-routes.md`, `docs/business-rules.md`,
   `docs/ai-integration.md`, `phases/phase-roadmap.md`, and `CLAUDE.md` so they match
   what was actually built — especially anywhere reality diverged from this plan. The
   scale-to-fit (non-aspect-preserving) rough-composite approach and the seam-only
   refinement strategy are both unvalidated against any real model call; if a future
   session with a real `GEMINI_API_KEY` finds either doesn't hold up, that correction
   goes in `docs/ai-integration.md`'s Mode F section and this phase file's own
   top-of-file reality check, not silently into code with no note.
6. Only say "Phase 20 Complete" when every checkbox is green and docs are in sync.

## Final Phase 20 Checklist

- [ ] `MIX` live end-to-end: schema, config, four-slot presign, ingest validation for
      two independent source/mask pairs, rough-composite, seam overlay conveyance,
      worker, generate-then-composite scoped to the seam, status/retry — all verified,
      not just written
- [ ] Rough-composite placement correctness proven programmatically on a fixture,
      independent of and prior to any provider-call test
- [ ] Off-seam pixel identity (against `rough_composite`, not either original source
      alone) proven programmatically on a fixture
- [ ] Non-aspect-preserving scale-to-fit behavior is pinned by a test and flagged as
      an open risk, not silently assumed acceptable
- [ ] No-QA-gate decision for MIX is documented as a fourth, distinct reason from
      MATCH's and RECOLOR's, not folded into either
- [ ] Self-audit passed with all green
- [ ] `docs/`, `phases/phase-roadmap.md`, `CLAUDE.md` updated to match what was
      actually built
- [ ] Manual verification done by architect

---

## Status after this phase

If built and verified as planned, this completes the three v3 feature phases named in
`phases/phase-roadmap.md`'s "Deferred to v3" table (MATCH, RECOLOR, MIX). No further
v3 feature phase is currently planned — the roadmap's remaining open items (Phase 11
observability, Phase 14 V1 decommission, Phase 17's blocked AWS account access, and
every still-open numbered decision) are the real remaining backlog, not further
image-operation phases. Do not generate a Phase 21 feature file speculatively; per
`phases/phase-roadmap.md`'s own rule 1, the next phase file is written against
whatever the client or roadmap actually calls for next, not invented ahead of it.
