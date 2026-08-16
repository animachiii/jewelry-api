# Phase 19 — RECOLOR (Masked Gemstone Recolor)

## Reality check before writing this

**State of the codebase as of 2026-08-16, after Phase 18 merged (PR #24):** four operations
live — `ANGLE_GENERATION`, `BACKGROUND_REMOVAL`, `BACKGROUND_REPLACEMENT`, `MATCH` — all
reusing one job/sub-job/asset state machine, one `GenerationProvider` seam
(`app/providers/gemini.py`, real SDK never exercised in tests — no `GEMINI_API_KEY` exists
in this environment, same gap every phase since 6 has hit), one rate limiter, one cost
ledger, one retry route (generalized in Phase 18 to handle >1 sub-job per job). Every prior
operation is **one-image-in** (plus, for `BACKGROUND_REPLACEMENT`, an optional second
reference image) **and the output either fully replaces the input's framing (Modes A/B) or
is accepted whole (Mode C, Mode D)** — nothing in this codebase has ever needed to preserve
*part* of an input image byte-for-byte while regenerating another part. RECOLOR is the
first operation that does.

**Why RECOLOR, not MIX, is next — same reasoning `phase-18-match.md` used for MATCH before
RECOLOR:** confirmed in `app/providers/gemini.py::_call_api`, the Gemini API takes only
`Part.from_text` and `Part.from_bytes` — **there is no mask parameter, no alpha-channel
input, nothing but images and text.** Both RECOLOR and MIX need a way to tell Gemini "change
only this region" despite that constraint, plus a server-side compositing step to guarantee
everything outside the edited region is byte-identical to the source. RECOLOR needs exactly
one mask; MIX needs two masks across two independent source images plus a seam-blend step.
Building RECOLOR first proves mask validation, mask-to-Gemini conveyance, and
generate-then-composite end to end against the simplest version of the problem — one mask,
one source, one output — the same "prove the new pattern on the smallest necessary
infrastructure first" reasoning Phase 18 used for MATCH before RECOLOR (no mask at all,
prove the fan-out shape first). MIX is a separate future phase; do not write its phase file
until this one is built and verified, per `phases/phase-roadmap.md` rule 1.

**Depends on Phase 18, Complete and merged.** Nothing here touches `sub_jobs.variant_index`
or `match_service.py`. What it *does* reuse directly: `orchestration.dispatch_job` (already
operation-agnostic — Phase 18 confirmed this reusing it unmodified for `MATCH`'s fan-out),
`status_rollup.compute_parent_status` (pure function over requested/succeeded/failed counts,
also already operation-agnostic), the generalized `POST /jobs/{job_id}/retry` (Phase 18's own
generalization already handles "retry every `FAILED` sub-job on the job, all-or-nothing" —
RECOLOR's job always has exactly one sub-job, so this is the same trivial "set of size 1" case
Phase 18 proved is a no-op for background operations), `cost_service.record_cost_event`,
`config_versions_repo`, `storage_service`, `image_validation.inspect_and_validate`.

**What's genuinely new, in order of how much this codebase has never done it before:**

1. A **mask asset** — a second uploaded file, structurally different from every prior upload
   (single-channel, binary-valued, dimension-locked to its source). No existing asset kind or
   validation path fits.
2. **Mask contract validation on ingest** — format, channel count, binary-only, dimension
   match, coverage bounds. All new. `422`, specific violation named, before any provider call
   — same "fail loud, name the exact thing" posture `docs/conventions.md` already requires,
   applied to a kind of input this codebase has never validated before.
3. **A mask-conveyance strategy that survives "no mask parameter exists."** The chosen
   approach (Step 3, below) is a colour overlay burned into the image sent to Gemini —
   unvalidated against real jewelry macro photography, because no real `GEMINI_API_KEY`
   exists in this environment to test it against. This is this phase's single biggest
   open risk, same category as Phase 9's uncalibrated QA thresholds and Phase 6's untested
   real SDK call — flagged, not solved, by writing good tests against fixtures.
4. **Generate-then-composite** — the *only* operation in this codebase where the provider's
   raw output is not the client-facing result. Everything outside the (eroded, feathered)
   mask must be byte-identical to the original upload. This needs real pixel-level work
   (Pillow, already a dependency, currently used only for structural validation in
   `image_validation.py` — this phase is its first real imaging use) and a correctness test
   that diffs pixels, not just an integration test asserting `COMPLETED`.
5. **A palette**, not free-form color input. `docs/business-rules.md` doesn't have a
   `RECOLOR` section yet; this phase adds one, modeled on how `MATCH` added `§14` without
   disturbing `§1`/`§13`.

---

## Step 1 — Schema

### What to do

New migration `0015_add_recolor_operation.py`. Two migrations Phase 18 needed were combined
into one (`ALTER TYPE ... ADD VALUE` + column/index DDL) because the new enum value was never
compared or inserted as data within that same migration's transaction — confirmed safe on
Postgres 15 (see `0013`'s own module docstring for the exact reasoning). The same reasoning
applies here, so this is one migration, not two — do not split it defensively; if a later
statement in this same migration ever needs to compare against `'RECOLOR'` as a value, split
at that point, not before.

1. `ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'RECOLOR'`.
2. `ALTER TYPE asset_kind_t ADD VALUE IF NOT EXISTS 'MASK'` — a mask is not an `INPUT` (it's
   not sent to the provider as reference material the way an `INPUT` image is — it's consumed
   server-side to build the overlay and drive compositing) and not an `OUTPUT`. It needs its
   own kind so `docs/schema.md`'s asset-kind semantics stay meaningful and so retention
   (Step 5) can give it a different lifetime than either.
3. Add `sub_jobs.mask_asset_id UUID NULL FK -> assets.id` — mirrors `input_asset_id` /
   `output_asset_id` exactly, same nullability posture (`NULL` for every non-`RECOLOR`
   operation, same as `variant_index` is `NULL` for everything except `MATCH`).
4. Add `sub_jobs.palette_code TEXT NULL` — the requested target color, validated against the
   active config's palette (Step 2). `NULL` for every other operation.
5. **Do not touch `ux_sub_jobs_job_single` or add a RECOLOR-specific partial index.** A
   `RECOLOR` job always has exactly one sub-job (same shape as background operations, not
   `MATCH`'s 1-4) — `angle IS NULL AND variant_index IS NULL` already covers it, no schema
   conflict exists here the way Phase 18's Step 1 had to solve for `MATCH`. Confirm this
   explicitly in the migration's docstring so a future reader doesn't assume every new
   operation needs an index change just because `MATCH` did.
6. Update `app/db/models/enums.py::Operation` and `AssetKind`, and the SQLAlchemy `SubJob`
   model (`mask_asset_id: Mapped[uuid.UUID | None]`, `palette_code: Mapped[str | None]`).
7. `jobs.requested_angles` is **not** reused for RECOLOR the way it was for background
   operations (`1`) and `MATCH` (`variant_count`) — a `RECOLOR` job always has exactly one
   sub-job, so set it to `1`, following the background-operations precedent exactly (not
   `MATCH`'s), and say so explicitly in `docs/schema.md`'s existing note on that column,
   which currently only lists three reinterpretations, not four.

### Checkpoint 1

- [ ] `alembic upgrade head` succeeds against a fresh `testcontainers` Postgres and is then
      confirmed live against the real Supabase database (`rsolykmjupiusdujajgj`) via
      `mcp__supabase__execute_sql` the same way Phase 18's merge was independently verified —
      `select unnest(enum_range(null::operation_t))` includes `RECOLOR`,
      `select unnest(enum_range(null::asset_kind_t))` includes `MASK`
- [ ] `alembic downgrade -1` succeeds and correctly leaves both new enum values in place
      (Postgres has no `DROP VALUE` — same documented posture `0013`'s `downgrade()` already
      established), dropping only the two new columns
- [ ] A test creates one `sub_jobs` row with `mask_asset_id` and `palette_code` set, `angle`
      and `variant_index` both `NULL`, under a `RECOLOR` job, and confirms
      `ux_sub_jobs_job_single` still allows it (nothing new needed) and still rejects a second
      angle-less, variant-less, mask-less sub-job under the same job
- [ ] SQLAlchemy model and live schema match exactly — autogenerate diff produces no changes
- [ ] `docs/schema.md`'s `sub_jobs` section gains `mask_asset_id`/`palette_code`; the
      `asset_kind_t` enum list and bucket-layout table both gain `MASK`; the
      `jobs.requested_angles` reinterpretation note gains RECOLOR as its fourth case

---

## Step 2 — Config: palette

### What to do

Following `0014`'s exact pattern (new `config_versions` row, `global.operations` object
extended without replacing `BACKGROUND_REMOVAL`/`BACKGROUND_REPLACEMENT`/`MATCH`, Redis
`config:active` cache invalidated, CLAUDE.md Hard Rule 11 — never mutate an existing
`config_versions` row):

```python
NEW_OPERATIONS = {
    "RECOLOR": {
        "enabled": True,
        "prompt": (
            "Recolor only the gemstone inside the region marked in solid magenta to "
            "{palette_prompt}. Do not alter any metal, prong, setting, or any part of "
            "the image outside the marked region. Preserve lighting, reflections, "
            "shadows, and composition exactly as they are."
        ),
        "unit_cost_usd": 0.02,  # placeholder, same uncalibrated status as every other seeded cost
    }
}
```

New top-level `global.palette` list, seeded in the same migration — a **new** config
substructure, not nested under `operations.RECOLOR` the way `background_presets` sits
alongside (not inside) `operations.BACKGROUND_REPLACEMENT`, matching that precedent exactly:

```python
PALETTE = [
    {"code": "EMERALD_GREEN", "label": "Emerald", "prompt_phrase": "a deep emerald green emerald", "is_active": True},
    {"code": "RUBY_RED", "label": "Ruby", "prompt_phrase": "a rich pigeon-blood red ruby", "is_active": True},
    {"code": "SAPPHIRE_BLUE", "label": "Sapphire", "prompt_phrase": "a vivid cornflower blue sapphire", "is_active": True},
    {"code": "AMETHYST_PURPLE", "label": "Amethyst", "prompt_phrase": "a rich violet-purple amethyst", "is_active": True},
    {"code": "CITRINE_YELLOW", "label": "Citrine", "prompt_phrase": "a warm golden-yellow citrine", "is_active": True},
]
```

**Raw hex is deliberately not supported** — a fixed palette gives predictable prompt phrasing
and avoids "flat unrealistic fill" results a literal hex-to-color instruction tends to
produce; the same reasoning the stale pre-Phase-0 planning docs already reached independently
is still correct here and is kept. `{palette_prompt}` is a second runtime template
substitution alongside `MATCH`'s `{target_category}` — reuse the same fail-loud
`str.format`/`KeyError` posture `job_service.resolve_match_prompt` already established
(`app/services/job_service.py::resolve_recolor_prompt`, new function, same shape).

### Checkpoint 2

- [ ] Migration `0016_add_recolor_config.py` inserts a new `config_versions` row with
      `RECOLOR` added to `payload.global.operations` (merged in, not replacing the three
      existing keys — same defensive check `0014`'s own migration had to get right) and a
      new `payload.global.palette` list; deactivates the prior version; invalidates the
      Redis `config:active` cache
- [ ] `find_operation_config(config_version, Operation.RECOLOR)` returns the seeded dict
- [ ] A unit test confirms `resolve_recolor_prompt` substitutes `{palette_prompt}` correctly
      for a known `palette_code`, and fails loudly (not silently) for a `palette_code` not
      present in the active config's palette — this is a request-time `422`, not the
      template-resolution `KeyError`; the two are different failure points and both need
      coverage (see Step 3's validation order)
- [ ] `GET /api/v2/config`'s response includes the active palette (`code` + `label` only —
      `prompt_phrase` stays internal, same "prompts never exposed to the client" rule
      `docs/api-routes.md` already states for angle/background prompts)

---

## Step 3 — Mask contract and ingest validation

### What to do

New `app/services/mask_validation.py`, sibling to `image_validation.py`, not a modification
of it — a mask is validated against entirely different rules (single-channel, binary,
dimension-locked to a *specific other image*) that would only complicate
`image_validation.inspect_and_validate`'s existing contract for ordinary photos. Reuses
`storage_service.download_to_temp` and Pillow, same as `image_validation.py`.

**Mask contract** — violated → `422 VALIDATION_ERROR` naming the specific rule, same error
code and specificity `docs/conventions.md` already mandates project-wide (not a new code;
`VALIDATION_ERROR`'s existing detail payload already carries a machine-readable reason,
following the exact shape `image_validation.InvalidImageError` uses):

| Rule | Check |
| :--- | :--- |
| PNG format | `Image.open(...).format == "PNG"` |
| Single-channel, 8-bit, no alpha | `img.mode == "L"` after open (reject `RGBA`/`P`/`RGB` outright — do not silently convert; a client sending a color image as a mask is a client bug worth surfacing, not papering over) |
| Dimensions exactly match the source image | Compare against the `RECOLOR` job's own uploaded source asset's `width_px`/`height_px` (already recorded by `image_validation.inspect_and_validate` when the source was ingested) |
| Binary values only (0 and 255) | `set(img.getdata())` (or a numpy-free equivalent — no numpy dependency exists in this project yet; do not add one for this alone if PIL's own histogram (`img.histogram()`) can answer "are there only two populated buckets, at 0 and 255" without a full pixel-set scan on a large image) is a subset of `{0, 255}` |
| Coverage between `MASK_MIN_COVERAGE_PCT` and `MASK_MAX_COVERAGE_PCT` | White-pixel fraction, config-driven bounds (new settings, defaults `0.5` and `60.0` — env-configurable per `docs/conventions.md`'s "tunable numeric behaviour is env-configurable" rule, not hardcoded) |

Validated **before** any provider call, same "no billable call on invalid input" posture
`docs/business-rules.md` §4's `INVALID_INPUT` row already establishes for corrupt images.

**Request/route shape**, mirroring `MatchRequest`/`app/api/v2/match.py` closely:

```python
class RecolorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_path: str        # source photo
    mask_storage_path: str   # the mask, same bucket
    palette_code: str
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

`POST /api/v2/uploads/presign` gains a third `operation` value, `"RECOLOR"` — but unlike
`MATCH` (one upload slot) it needs **two**: extend the existing `operation`-mode response
with a second slot, `mask_upload`, alongside `operation_upload`, gated the same way
`include_background_upload` gates `background_upload` for `BACKGROUND_REPLACEMENT` — i.e.
`operation: "RECOLOR"` always returns both slots (a RECOLOR job is never valid without a
mask, unlike a custom background photo, which is optional), so no extra request flag is
needed, just a route-level `if operation == Operation.RECOLOR` branch returning both.

`POST /api/v2/recolor`, new route, same shape as `background.py`'s single-operation routes:

**Validation, in order — all `4xx` before any job row is created:**

1. `operations.RECOLOR.enabled` (`422 OPERATION_DISABLED`, same code every operation uses)
2. `palette_code` exists and `is_active` in the active config's palette
   (`422 PALETTE_NOT_FOUND` / `422 PALETTE_INACTIVE` — two new `ErrorCode` values, same
   naming convention `PRESET_NOT_FOUND`/`PRESET_INACTIVE` already set for background presets)
3. `storage_path` exists in `jewelry-inputs`, belongs to this client, passes
   `image_validation.inspect_and_validate` (`422 ASSET_NOT_FOUND` / `ASSET_NOT_OWNED` /
   `VALIDATION_ERROR`, all existing codes)
4. `mask_storage_path` exists in `jewelry-inputs`, belongs to this client, passes
   `mask_validation.validate_mask` against the source's dimensions from step 3
   (`422 ASSET_NOT_FOUND` / `ASSET_NOT_OWNED` / `VALIDATION_ERROR` with a mask-specific
   detail reason — reuses `VALIDATION_ERROR`, does not invent a new code, per this step's
   own reasoning above)

### Checkpoint 3

- [ ] `mask_validation.validate_mask` has one dedicated test per contract rule (6 rules
      above), each asserting the *specific* violation is named in the raised error's detail
      — not a generic "invalid mask" message, per `docs/conventions.md`
- [ ] `POST /api/v2/uploads/presign` with `{"operation": "RECOLOR"}` returns both
      `operation_upload` and `mask_upload` slots
- [ ] `POST /api/v2/recolor` with a valid source + valid mask + active `palette_code` creates
      one job (`operation: RECOLOR`, `requested_angles: 1`, `category_code: NULL` — same as
      background operations, RECOLOR has no category) and one sub-job (`angle: NULL`,
      `variant_index: NULL`, `mask_asset_id` set, `palette_code` set)
    - [ ] A mask whose dimensions don't match the source returns `422` naming both the mask's
      actual dimensions and the source's, same specificity precedent
      `docs/conventions.md` requires (mirrors the stale pre-Phase-0 planning docs' example
      error message shape, which remains a good model even though those docs are otherwise
      superseded)
- [ ] `palette_code` not in the active config's palette returns `422 PALETTE_NOT_FOUND`;
      an inactive one returns `422 PALETTE_INACTIVE`
- [ ] Idempotency: replaying `(client_id, Idempotency-Key)` with an identical body returns
      the original `job_id`, `200`; a different body under the same key returns `409` — same
      test shape every other job-creating route already has
- [ ] `docs/api-routes.md` gets a new `## Masked Gemstone Recolor (Phase 19)` section and
      the Uploads section documents the new `mask_upload` slot, same structure as the
      Companion-Piece Generation section Phase 18 added

---

## Step 4 — Mask-to-Gemini conveyance and worker

### What to do

New `app/services/recolor_service.py`, mirroring `match_service.py`'s
rate-limit→provider-call→cost-event→success/fail→recompute shape closely, plus the two
genuinely new steps this operation needs on either side of the provider call:

**Before the call — build the overlay image, server-side, never sent to the client:**

1. Download source and mask bytes.
2. Erode the mask by `MASK_ERODE_PX` (new setting, default `2`) — pulls the mask edge back
   off metal/prongs a hand-drawn or auto-generated mask routinely catches at its boundary.
   Use Pillow's `ImageFilter.MinFilter` (erosion via minimum filter over a binary mask) — no
   new imaging dependency; `Pillow` is already in `pyproject.toml` and this phase is simply
   its first real (non-validation) use.
3. Composite a solid magenta fill (`(255, 0, 255)`, fully opaque — a color vanishingly
   unlikely to appear in real jewelry photography, chosen for the same reason a chroma-key
   color is chosen) over the source through the eroded mask, at the source's working
   resolution. This is the single image sent to Gemini as `reference_images[0]` — the prompt
   (Step 2) instructs it to recolor "the region marked in solid magenta."
4. Feather the mask separately by `MASK_FEATHER_PX` (new setting, default `3`) — **not**
   applied to the overlay sent to Gemini (a hard-edged instruction region is what the model
   should see), only to the alpha channel used for the *compositing* step after generation,
   so the seam between original and recolored pixels blends rather than shows a hard ring.

**The call itself:** `provider.generate(prompt, [overlay_bytes], seed)` — same
`GeminiProvider`, same rate limiter, same cost-event-before-evaluation posture
`match_service.py`/`background_service.py` already established. No provider code changes.

**After a successful call — generate-then-composite, the step no other operation in this
codebase has needed:**

5. Composite the provider's raw output back onto the **original, un-eroded-mask, full**
   source image, using the feathered mask (step 4) as the alpha blend — `Image.composite`
   with the feathered mask as the mask argument. Everywhere the mask is `0`, the output pixel
   must be the original source's pixel, exactly.
6. Store *this* composited image as the `OUTPUT` asset — the client never sees Gemini's raw
   frame. This is the one deliberate exception to "the provider's response is the artifact
   stored" every prior operation follows; document it as such in this module's docstring the
   way `match_service.py`'s docstring documents its own deliberate differences from
   `background_service.py`.

**No QA gate**, deliberately — but for a different reason than `MATCH`'s. `MATCH`'s output
is *supposed* to differ from its source (§7's existing reasoning). RECOLOR's output is
supposed to be **provably identical to the source outside the mask** — that's not a
similarity-*score* question the existing `GeminiQaProvider` (a perceptual-similarity judge)
is suited to answer; it's a **pixel-exact compositing correctness** question, verified by a
deterministic test (Checkpoint 4, below), not a probabilistic model call. Ship straight to
`COMPLETED` on successful compositing, same terminal shape as `MATCH` and Mode A, for a third,
distinct reason — write this distinction explicitly into `docs/business-rules.md`'s new §15,
the same way §7 already distinguishes three different "no QA gate" reasons (Mode A, MATCH)
rather than letting a fourth blur into an existing one.

**Recorded on every call** (to `sub_jobs`): `prompt_snapshot`, `model_version`, `seed` — same
fields, same reason, every prior operation already establishes.

### Checkpoint 4

- [ ] A real `POST /api/v2/recolor` against `testcontainers` Postgres + real local Redis +
      real Supabase Storage runs to `COMPLETED`, fixture-driven Gemini (no real
      `GEMINI_API_KEY` exists in this environment, same posture every prior phase's
      verification has used)
- [ ] **Off-mask pixel identity, asserted programmatically, not visually:** given a fixture
      source, a fixture mask, and a fixture Gemini response image (a different, deterministic
      fixture image simulating "the model changed everything"), the final composited `OUTPUT`
      asset's pixels **outside the original mask's white region** are byte-identical to the
      source's own pixels at every one of those coordinates. This is the single most important
      test in this phase — it's the only thing that actually proves generate-then-composite
      works, and nothing else in this checkpoint list substitutes for it
- [ ] A test with a deliberately sloppy fixture mask that overlaps a simulated "metal" region
      in the fixture confirms erosion (`MASK_ERODE_PX`) shrinks the region actually painted
      magenta in the overlay sent to the provider, relative to the raw mask
    - [ ] Real prong-bleed calibration (does erosion actually keep metal safe on **real**
      jewelry photography) is **not achievable in this environment** — no real
      `GEMINI_API_KEY` or real client mask exists to test against, same category of gap as
      the uncalibrated QA thresholds (roadmap open decision #8) and MATCH's uncalibrated
      prompt/pricing (open decision #12). Flag this explicitly as a new open decision, do not
      claim it's solved because the erosion *code* is tested
- [ ] A `cost_events` row is written per provider call attempt, `operation: "recolor"`, same
      as every other operation
- [ ] `POST /api/v2/jobs/{job_id}/retry` works unchanged against a `FAILED` RECOLOR job —
      Phase 18's all-or-nothing generalization already covers a job with exactly one sub-job
      as a trivial case; add one regression test confirming this rather than assuming it from
      Phase 18's own background-operations regression test

---

## Step 5 — Retention and docs

### What to do

- **Retention** (`docs/business-rules.md` §11): add `MASK` at **7 days** — it's derivable
  from nothing (unlike `MATTE`, whose 30-day/regenerable-from-input note doesn't apply here;
  a mask is a client-drawn artifact, not a byproduct the system could recreate), but it also
  has no purpose once its one job is terminal, so it doesn't need `INPUT`'s 90-day
  retry-window justification either. Update `app/services/retention_policy.py`'s
  `compute_expires_at` to branch on `AssetKind.MASK` the same way it already branches per
  kind for `INPUT`/`MATTE`/`OUTPUT`.
- `docs/ai-integration.md` — add **Mode E — Masked gemstone recolor (RECOLOR, Phase 19)**
  under Call site 1, alongside Modes A-D, following Mode D's exact structure (trigger/where/
  input/output table, then prose). State plainly the reasoning Step 4 above lays out: unlike
  every other mode, the client-facing output is not the provider's raw response — it's a
  server-side composite, and that's the one new architectural fact this mode introduces to
  the doc.
- `docs/business-rules.md` — new **§15 RECOLOR (Phase 19)**, modeled on §14's structure
  (operation matrix table, bullet list of rule deltas, explicit QA-gate reasoning
  cross-referenced from §7). Also extend §1's opening line ("There are exactly four
  angles...") with the same kind of explicit carve-out §1 already has for MATCH — RECOLOR has
  no category at all (like background operations), so the carve-out is simpler: "RECOLOR has
  no category_code and no angle matrix; see §15."
- `docs/api-routes.md` — new `## Masked Gemstone Recolor (Phase 19)` section (Step 3), and the
  Uploads section's `operation`-mode bullet list gains the `RECOLOR` → `mask_upload` case.
- `docs/schema.md` — already covered in Step 1's checkpoint; confirm at self-audit time that
  the retention/bucket-layout table also reflects `MASK`'s new 7-day row.
- `phases/phase-roadmap.md` — add this phase's row, following the exact format Phase 18's row
  used (state what's real, what's still placeholder, what's explicitly not done).
- `CLAUDE.md` — extend the operation-family enumeration line Phase 18 already added a
  sentence to, if the current text lists three-then-four; make it five.

### Checkpoint 5

- [ ] Every doc listed above reflects what was actually built, not this phase file's plan —
      cross-check field names, error codes, and section numbers against real code before
      writing, same self-audit discipline every prior phase file requires
- [ ] `docs/ai-integration.md`'s Mode E section explicitly states the
      "client-facing output is a server-side composite, not the provider's raw response"
      distinction

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file.
2. Test each one for real — against `testcontainers` Postgres, real local Redis, real
   Supabase Storage, fixture-driven Gemini, same stack every prior phase has used. Apply and
   independently verify both migrations against a copy of the real live database, the same
   way Phase 18's merge was checked via `mcp__supabase__execute_sql` rather than trusted from
   the phase file's checkboxes alone.
3. Return a structured report:
   ✅ [Checkpoint] — Pass
   ⚠️ [Checkpoint] — Partial: [specific reason]
   ❌ [Checkpoint] — Fail: [specific reason]
4. Fix all failures and partials before reporting phase complete.
5. Update `docs/schema.md`, `docs/api-routes.md`, `docs/business-rules.md`,
   `docs/ai-integration.md`, `phases/phase-roadmap.md`, and `CLAUDE.md` so they match what was
   actually built — especially anywhere reality diverged from this plan. The mask-conveyance
   approach (colour overlay) in particular is unvalidated against any real model call; if a
   future session with a real `GEMINI_API_KEY` finds it doesn't hold up, that correction goes
   in `docs/ai-integration.md`'s Mode E section and this phase file's own top-of-file reality
   check, not silently into code with no note.
6. Only say "Phase 19 Complete" when every checkbox is green and docs are in sync.

## Final Phase 19 Checklist

- [ ] `RECOLOR` live end-to-end: schema, config/palette, mask contract validation, presign,
      route, overlay conveyance, worker, generate-then-composite, status/retry — all verified,
      not just written
- [ ] Off-mask pixel identity proven programmatically on a fixture, not asserted from
      "the test suite is green"
- [ ] Erosion behavior tested against a synthetic sloppy-mask fixture; real prong-bleed
      calibration explicitly flagged as unvalidated (new open decision), not silently skipped
- [ ] No-QA-gate decision for RECOLOR is documented as a third, distinct reason from Mode A's
      and MATCH's, not folded into either
- [ ] Self-audit passed with all green
- [ ] `docs/`, `phases/phase-roadmap.md`, `CLAUDE.md` updated to match what was actually built
- [ ] Manual verification done by architect

---

## Note for the next v3 phase

**MIX** is next. It needs everything this phase builds (mask contract, erosion/feathering,
generate-then-composite) applied across **two** independent source images and **two** masks,
plus a step this phase deliberately doesn't need: a deterministic rough-composite (crop
region B via its mask, scale/align it into region A via A's mask, using each asset's stored
`width_px`/`height_px`) *before* any provider call, followed by a refinement call scoped only
to blending the visible seam — not a raw two-source-two-mask generation request, since
cross-image spatial reasoning is the weakest capability in play for any current-generation
image model. Do not generate that phase file until this one is actually built and verified —
its mask-validation module, erosion/feathering constants, and generate-then-composite pattern
are exactly what MIX will extend, and writing MIX's phase file against an unbuilt RECOLOR
risks assuming a mask-conveyance approach or compositing helper signature that turns out
different in practice, the same risk this phase's own top section flags for its untested
overlay strategy.
