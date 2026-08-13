# Custom Background Compositing — Design

**Date:** 2026-08-13
**Status:** Approved, implemented

## Problem

`POST /api/v2/background/replace` (Phase 15) only supports a curated list of
backdrop presets (`background_presets`, currently just `STUDIO_WHITE`). The
client wants to supply their own background photograph and have the product
composited into it realistically — matched lighting, color temperature, and
shadows — not a visibly pasted cutout.

This does **not** reopen `docs/decisions/0002-background-removal-approach.md`.
That decision ruled out an alpha-channel/transparent-cutout output; a custom
background is a different *source* for the same flat/solid-composite
operation `BACKGROUND_REPLACEMENT` already performs. No alpha channel is
needed either way — Gemini receives two images and returns one flattened
result, same shape as the existing preset path.

## Scope decisions (confirmed with the client)

- **Extends the existing `/background/replace` endpoint and `BACKGROUND_REPLACEMENT`
  operation** — not a new operation type. `preset_code` and a new
  `background_storage_path` are mutually exclusive alternatives on the same
  request.
- **One-off per job.** The uploaded background photo is not saved or reusable
  across jobs — no new "background library" entity, no CRUD routes. Same
  lifecycle/retention as the product input photo (90 days, `INPUT` asset kind).
- **Single combined Gemini call.** One call receives both images (product +
  background) and a compositing prompt; one image comes back. Rejected a
  two-step remove-then-composite pipeline as unnecessary cost/latency/failure
  surface with no evidence of a realism benefit — revisit only if single-call
  quality proves insufficient in production.

## API contract changes

### `POST /api/v2/background/replace`

Request:
```json
{
  "storage_path": "...",
  "preset_code": "...",           // now optional
  "background_storage_path": "...", // new, optional
  "sku_reference": "...",
  "metadata": {}
}
```

Exactly one of `preset_code` / `background_storage_path` must be present —
`422 VALIDATION_ERROR` if both or neither are given.

**Validation, in order** (extends the existing list in
`docs/api-routes.md`):

1. `operations.BACKGROUND_REPLACEMENT.enabled` is `true`
2. Exactly one of `preset_code` / `background_storage_path` given (new)
3. If `preset_code`: exists and active (`422 PRESET_NOT_FOUND` /
   `PRESET_INACTIVE`, unchanged)
4. `storage_path` (product photo) exists, owned by client, passes
   `image_validation.inspect_and_validate` (unchanged)
5. If `background_storage_path`: same ownership + `image_validation` checks,
   parallel to step 4 (new)

Response shape unchanged.

### `POST /api/v2/uploads/presign`

Operation-mode request gains an optional flag:

```json
{ "operation": "BACKGROUND_REPLACEMENT", "include_background_upload": true }
```

Response gains a second upload slot, `background_upload`, same shape as the
existing `operation_upload` (`upload_url` / `storage_path` / expiry). One
presign call covers both uploads — no second round trip.

## Data model changes

New migration:

- `sub_jobs.background_asset_id` — nullable `UUID FK → assets.id`, same
  pattern as the existing `matte_asset_id` / `output_asset_id` columns. Set
  only when the job used a custom background; `NULL` for the preset path.
  The background photo is stored as an ordinary `INPUT`-kind asset scoped to
  that sub-job — no new `asset_kind_t` value needed, the two input assets are
  distinguished by which FK column on `sub_jobs` points at them.

`jobs.preset_code` is unchanged in type but now genuinely optional at the
business-rule level (already nullable in schema) — `NULL` when
`background_asset_id` is set instead.

No new table. No new `operation_t` value.

## Worker & prompt changes

`app/services/background_service.py::process` — when
`sub_jobs.background_asset_id` is set, downloads both the product input
asset and the background asset and calls `GeminiProvider` with **both**
images instead of one.

`app/providers/gemini.py::GeminiProvider` — call signature already accepted
an optional `reference_images: list[bytes]` param, additive only. Existing
Mode A/B/C call sites (single image) are unaffected.

`_resolve_prompt` branches on which source is active:

- **Preset path (unchanged):** operation prompt + preset's own prompt,
  concatenated.
- **Custom-background path (new):** operation prompt + a new config value,
  `config.global.operations.BACKGROUND_REPLACEMENT.custom_background_prompt`,
  seeded via migration alongside `background_asset_id`. Default prompt
  instructs: place the product naturally into the supplied background as if
  it were photographed there — match the background's light direction, color
  temperature, and perspective; render realistic contact shadows and any
  relevant surface reflections; do not composite a visibly pasted cutout.

Everything downstream is unchanged, since it was already written against
`operation` rather than the background source:

- Still always enters `QA_REVIEW` on success (never straight `COMPLETED`) —
  same subject-preservation gate, same
  `background_qa_similarity_threshold`, same reference image (the **product**
  input photo, never the background photo).
- Same cost resolution
  (`operations.BACKGROUND_REPLACEMENT.unit_cost_usd`) — still one
  `cost_events` row per call, since it's still one Gemini call regardless of
  how many images go in.
- Same retry path (`POST /jobs/{job_id}/retry`) — re-downloads whichever
  assets are linked (one or two) and re-runs. No special-casing needed.

## UI changes (`ui/index.html`)

The "Replace background" row gains a **Background source** selector:
`Preset` (default — current behavior byte-identical) / `Custom upload`.
Selecting `Custom upload` hides the existing preset dropdown and reveals a
second file input, **Background photo**, next to the existing product
**Photo** field.

`updatePresetVisibility` becomes a three-way toggle (operation × source)
instead of today's two-way one.

Submit logic (`bg-submit-btn` handler):

- **Custom-upload path:** presign request adds
  `include_background_upload: true`; uploads both files to their respective
  presigned URLs (product → `operation_upload`, background →
  `background_upload`); submits `background_storage_path` instead of
  `preset_code`.
- **Preset path:** unchanged.

Client-side validation before submit: custom-upload path requires a
background file to be chosen, mirroring the existing preset-required check.

## Testing

Follows the existing Phase 15 pattern — `tests/fixtures/gemini/` gets a new
two-image-input fixture; no live Gemini calls in CI
(`docs/ai-integration.md`'s testing rules, unchanged). New tests needed:

- `/background/replace` rejects both-given and neither-given
  `preset_code`/`background_storage_path` with `422 VALIDATION_ERROR`.
- `/uploads/presign` with `include_background_upload: true` returns both
  upload slots.
- `background_service.process` calls `GeminiProvider` with two images when
  `background_asset_id` is set, one image otherwise (regression-guards the
  preset path stays unaffected).
- Retry re-downloads both assets when both are linked.
- `GET /qa/review-queue` still resolves correctly for a custom-background
  item (reference image is the product photo, not the background).

## Out of scope (explicitly, not silently)

- Reusable/saved background library — one-off only, per the scope decision
  above. Revisit as a new decision if the client asks for it later.
- Two-step remove-then-composite pipeline — not built unless single-call
  quality proves insufficient.
- Any change to `docs/decisions/0002-background-removal-approach.md`'s
  no-alpha-channel decision — this feature does not need one.
