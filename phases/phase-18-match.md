# Phase 18 — MATCH (Companion-Piece Generation)

## Reality check before writing this

First v3 feature phase, and it deliberately runs **independent of Phase 17**. AWS deployment and application feature work don't depend on each other — `POST /api/v2/match` works identically on Render, Fly, or App Runner, whichever is live when this lands. Do not block this phase on AWS access being unblocked.

**Depends on Phase 16, which is Complete and verified live** — the job/sub-job state machine this phase extends now has task time limits, a reconciliation sweep, and a real `OUTPUT` retention value. Building a new operation type on top of that is exactly the situation Phase 16 existed to set up.

**Why MATCH first, before RECOLOR or MIX:** it has no mask. `RECOLOR` and `MIX` both need a mask-conveyance strategy (the Gemini API has no mask parameter — see the note in `docs/ai-integration.md`'s Call site 1 about what "the provided image" prompting actually means) and a compositing step to preserve everything outside the masked region. That's real, separate infrastructure, worth its own phase once this one proves the fan-out and provider-call shape for a **new** operation end to end. MATCH also fits this codebase's own precedent for what "let data decide" means in practice — it's the cheapest way to find out whether Gemini can hold style consistency across two independently-generated pieces before committing to the harder masked operations.

**This phase follows Phase 15's shape closely, on purpose** — `BACKGROUND_REMOVAL`/`BACKGROUND_REPLACEMENT` are the only precedent this codebase has for "a new operation reusing the existing job/sub-job/asset/status machinery," and the fit is good. Read `phases/phase-15-background-operations.md` and `app/services/background_service.py` before starting — most of this phase is "do the same thing, for a different operation and a different fan-out shape," not new architecture.

**One real schema conflict Phase 15 didn't have to solve, and this phase does:** `sub_jobs` has a partial unique index, `ux_sub_jobs_job_single — (job_id) unique where angle IS NULL`, built for background operations where exactly one angle-less sub-job exists per job. MATCH needs `variant_count` angle-less sub-jobs per job (1–4). Applying `POST /api/v2/match` with `variant_count: 2` against the current schema would violate that index on the second sub-job insert. Step 1 below fixes this — it isn't optional and isn't safe to discover at insert time in production.

---

## Step 1 — Schema

### What to do

New Alembic migration (`0013_add_match_operation.py`):

1. `ALTER TYPE operation_t ADD VALUE 'MATCH'` — in its own migration, not combined with other DDL (Postgres requires enum additions to commit separately from other schema changes in the same transaction in the versions this project has hit before; `0006`'s own migration already establishes this pattern for `operation_t`, follow it exactly).
2. Add `sub_jobs.variant_index INT NULL` — 0-based position within a MATCH job's requested variants. `NULL` for every other operation, mirroring how `angle` is `NULL` for background operations.
3. Replace the existing `ux_sub_jobs_job_single` index. It stays exactly as-is for background operations (still exactly one angle-less, variant-less sub-job per job there) but needs a sibling for MATCH:
   - Keep `ux_sub_jobs_job_single` but narrow its `WHERE` clause to `angle IS NULL AND variant_index IS NULL` — this is what actually preserves "exactly one sub-job" for `BACKGROUND_REMOVAL`/`BACKGROUND_REPLACEMENT` once `variant_index` exists as a column those rows will also have (as `NULL`).
   - Add `ux_sub_jobs_job_variant — (job_id, variant_index) UNIQUE WHERE variant_index IS NOT NULL` — the MATCH equivalent of `ux_sub_jobs_job_angle`.
4. `jobs.requested_angles` is reused for MATCH exactly as it already was for background operations — it becomes "count of sub-jobs requested," value = `variant_count` (1–4). No new column. Update its `docs/schema.md` note to add MATCH to the list of operations that reinterpret this column's name (it already documents this happening once, for background operations; this is the second, not a new precedent).
5. Update `app/db/models/enums.py::Operation` and the SQLAlchemy `SubJob` model (`variant_index: Mapped[int | None]`) to match.

### Checkpoint 1

- [ ] `alembic upgrade head` succeeds against a fresh testcontainers Postgres and against a copy of the real live database
- [ ] `select unnest(enum_range(null::operation_t))` includes `MATCH`
- [ ] A test inserts two `variant_index=0` and `variant_index=1` sub-jobs with `angle IS NULL` under the same `job_id` and both succeed
- [ ] A test confirms `ux_sub_jobs_job_single`'s narrowed `WHERE` clause still rejects a second angle-less, variant-less sub-job under one job (the background-operation invariant is unbroken)
- [ ] SQLAlchemy model and live schema match exactly — autogenerate diff produces no changes
- [ ] `docs/schema.md`'s `sub_jobs` and `jobs` sections updated with `variant_index` and the reused-column note

---

## Step 2 — Config

### What to do

Following `0007`'s exact pattern (insert a new `config_versions` row, never mutate the active one — CLAUDE.md Hard Rule 11; carry `global` forward through `config_sync_service.normalize_sheet_rows`, since the real Google Sheet has no Global tab and never will for this key, same reasoning `0007`'s docstring already gives for background-operation keys):

```python
NEW_OPERATIONS = {
    "MATCH": {
        "enabled": True,
        "prompt": (
            "Using the provided jewelry piece as a style reference, design a "
            "matching {target_category} intended to be worn as part of the same "
            "set. Match the metal tone, stone type and cut, and overall design "
            "language exactly. Render as a standalone studio product photograph "
            "on a clean white background, camera angle and lighting consistent "
            "with a real product catalog shot. Do not reproduce the reference "
            "piece itself — generate a new, different item that belongs with it."
        ),
        "unit_cost_usd": 0.02,  # placeholder, same status as every other seeded cost — not a real price yet
    }
}
```

Same explicit placeholder caveat `0007` already states for its own seeded prompts: this prompt hasn't been reviewed by the client, and the same "not a value to ship to real client traffic without review" note applies. `{target_category}` is a real template substitution, resolved from the request's `category_code` at prompt-build time — this is new; no existing operation's prompt has a runtime-substituted field, so `_resolve_prompt`-equivalent logic for MATCH needs to actually perform the substitution, not just concatenate the way `BACKGROUND_REPLACEMENT`'s preset-appending does.

### Checkpoint 2

- [ ] Migration `0014_add_match_config.py` inserts a new `config_versions` row with `MATCH` added to `payload.global.operations`, deactivates the prior version, invalidates the Redis `config:active` cache — mirrors `0007`'s own migration body
- [ ] `find_operation_config(config_version, Operation.MATCH)` returns the seeded dict
- [ ] A unit test confirms `{target_category}` is substituted with the request's `category_code` (e.g. `EARRING`) in the resolved prompt, and that an unresolvable placeholder (a template referencing a variable not present in the request) fails loudly rather than shipping a prompt with a literal `{target_category}` string to Gemini — same "fail loud, don't silently degrade" posture `_resolve_prompt`'s preset branch already uses

---

## Step 3 — Request schema, presign, and route

### What to do

`app/api/v2/schemas/match.py`, same shape as `app/api/v2/schemas/background.py`:

```python
class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    target_category: str  # validated against active config's category list, same as /generate's category_code
    variant_count: int = Field(default=1, ge=1, le=4)
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Extend `POST /api/v2/uploads/presign`'s existing `operation` mode (`docs/api-routes.md`'s Uploads section) to accept `"operation": "MATCH"` alongside the existing two background values — same single-upload response shape (`operation_upload`), no new response field needed, since MATCH only ever has one source image regardless of `variant_count`.

`app/api/v2/match.py`, mirroring `app/api/v2/background.py`:

- `POST /api/v2/match` — validates `operations.MATCH.enabled`, `storage_path` ownership and existence, `target_category` against the active config's category list (**not** `operations.MATCH`'s own list — categories are a single source of truth, `payload.categories`, shared across every operation that references one), image validation, `Idempotency-Key`. Creates the job with `operation=MATCH`, `category_code=target_category`, `requested_angles=variant_count`, then creates `variant_count` sub-jobs with `angle=NULL`, `variant_index=0..variant_count-1`.
- Returns `202` with `JobAcceptedResponse` — same envelope every other job-creating route already returns, with a `variants` array (parallel to `/generate`'s `angles` array and `/background/*`'s single-element `angles`) instead of introducing a third response shape.

### Checkpoint 3

- [ ] `POST /api/v2/match` with `variant_count: 3` creates one job and three sub-jobs, each with a distinct `variant_index` and `angle IS NULL`
- [ ] `target_category` not present in the active config's category list returns `422` with a specific error code, not a generic validation failure
- [ ] `operations.MATCH.enabled: false` in the active config returns `422 OPERATION_DISABLED`, same code background operations already use for the same condition
- [ ] Idempotency: replaying the same `Idempotency-Key` + identical body returns the original `job_id`; a different body under the same key returns `409` — same test shape `docs/business-rules.md` §8 already specifies for `/generate`
- [ ] `POST /api/v2/uploads/presign` with `{"operation": "MATCH"}` returns a valid `operation_upload` slot
- [ ] `docs/api-routes.md` gets a new `## Companion-Piece Generation (Phase 18)` section, same structure as the existing `## Background Operations` section

---

## Step 4 — Worker and fan-out

### What to do

New `app/services/match_service.py`. Two things it needs that `background_service.py` didn't, because background operations are always exactly one sub-job and MATCH is 1–4:

- **Fan-out on job creation.** `orchestration_service.dispatch_job` already solves "create N sub-jobs, dispatch N Celery tasks, mark the parent `PROCESSING`, roll up to `PARTIAL_SUCCESS`/`COMPLETED`/`FAILED` as each finishes" for the angle case. Reuse that dispatch shape for MATCH's variants rather than `background_service`'s single-dispatch shape — this is closer to angle generation's orchestration problem than to background operations', despite MATCH's schema changes (Step 1) being modeled on background operations' angle-less pattern. Confirm with `app/workers/orchestration.py` exactly what's generic enough to reuse directly versus what needs a MATCH-specific wrapper (e.g. if `dispatch_job` is written assuming `angle_t` fan-out specifically, it may need a small generalization to fan out over `variant_index` instead — make that change explicit if so, don't silently duplicate the whole dispatch function).
- **A single `input_asset_id`, shared conceptually across all variants but recorded per sub-job** the same way angle generation records the same source per angle — no new asset-linking pattern needed, `sub_jobs.input_asset_id` already supports this.

`app/workers/match.py`, mirroring `app/workers/background.py`'s thin-wrapper shape — the real logic lives in `match_service.py`, testable without Celery, same split every other worker module already follows.

**QA gate — deliberately not added in this phase.** Background operations always enter `QA_REVIEW` because, per `docs/ai-integration.md`, "the cutout *is* the product" — a similarity-to-source score is a meaningful signal there. MATCH's output is *supposed* to differ from its source (it's a different piece); a perceptual-similarity gate built for "did this stay faithful to the input" is the wrong tool for "does this look like a matching companion piece," which is a stylistic-consistency judgment the existing `GeminiQaProvider` isn't built to make. Ship MATCH straight to `COMPLETED` on a successful provider call, same posture Mode A angle generation originally launched with — this project's own "Deferred to v3" table already established the precedent of letting real output quality decide whether a QA mechanism is worth building, rather than building one speculatively. Note this explicitly in `docs/business-rules.md` §7 (QA gate) as a deliberate scope decision for this phase, not an oversight, the same way `docs/ai-integration.md` already notes Mode A's own QA-gate omission as an accepted risk.

### Checkpoint 4

- [ ] A real `POST /api/v2/match` with `variant_count: 2` against `testcontainers` Postgres + real local Redis + real Supabase Storage runs to a terminal job status, with both sub-jobs dispatched and completing independently (fixture-driven Gemini, consistent with every prior phase's verification posture — no real `GEMINI_API_KEY` exists in this environment)
- [ ] One variant succeeding and one failing (forced via fixture) rolls the parent job up to `PARTIAL_SUCCESS`, reusing the exact rollup logic `docs/business-rules.md` §3 already specifies — no MATCH-specific rollup code written
- [ ] `POST /api/v2/jobs/{job_id}/retry` (job-level retry, `docs/api-routes.md`) works unchanged against a `PARTIAL_SUCCESS` MATCH job — retries only the failed variant(s)
- [ ] `GET /api/v2/status/{job_id}` for a MATCH job returns a `variants` array with per-variant status, output URL, and `variant_index` — mirrors the `angles`/`results` pattern already documented
- [ ] A `cost_events` row is written per provider call attempt, `operation: "match"`, same as every other operation

---

## Step 5 — Docs

### What to do

- `docs/ai-integration.md` — add **Mode D — Companion-piece generation (MATCH)** under Call site 1, alongside Modes A/B/C. State plainly that unlike Modes A–C, the source image is a *style reference*, not the subject being transformed — the output is a different physical piece, which is why no compositing or subject-preservation logic applies here the way it does for the other three modes.
- `docs/business-rules.md` §1 (currently "Angle matrix") — either rename to reflect that it now also governs MATCH's category validation, or add a new short section cross-referencing it; whichever reads cleaner once written, not decided here.
- `docs/api-routes.md` — new section per Step 3.
- `phases/phase-roadmap.md` — add this phase's row, following the same table format as 16/17.
- `CLAUDE.md` — one line under Project Overview noting a fourth operation family exists (angle generation, background operations, and now companion-piece generation), if the current text enumerates them.

### Checkpoint 5

- [ ] Every doc listed above reflects what was actually built, not this phase file's plan — cross-check field names, error codes, and section numbers against the real code before writing
- [ ] `docs/ai-integration.md`'s Mode D section explicitly states the "style reference, not subject" distinction and why compositing doesn't apply

---

## Self-Audit Instruction

Before declaring this phase complete, you must:

1. Re-read every checkpoint in this phase file.
2. Test each one for real — against testcontainers Postgres, real local Redis, real Supabase Storage, fixture-driven Gemini, same stack every prior phase has used. If a live Render/Supabase check is meaningful and low-cost (e.g. confirming the migration applies cleanly against a copy of the real database), do it, the same way Phase 16 did.
3. Return a structured report:
   ✅ [Checkpoint] — Pass
   ⚠️ [Checkpoint] — Partial: [specific reason]
   ❌ [Checkpoint] — Fail: [specific reason]
4. Fix all failures and partials before reporting phase complete.
5. Update `docs/schema.md`, `docs/api-routes.md`, `docs/business-rules.md`, `docs/ai-integration.md`, `phases/phase-roadmap.md`, and `CLAUDE.md` so they match what was actually built — not this phase file's plan, especially anywhere reality diverged (a different reuse of `orchestration_service.dispatch_job` than sketched in Step 4 is the most likely place this happens; write down what was actually needed).
6. Only say "Phase 18 Complete" when every checkbox is green and docs are in sync.

## Final Phase 18 Checklist

- [ ] `MATCH` operation live end-to-end: schema, config, route, presign, fan-out, worker, status/retry — all verified, not just written
- [ ] The `ux_sub_jobs_job_single` index conflict is resolved and proven not to block multi-variant jobs, while still protecting the background-operation invariant
- [ ] QA-gate omission for MATCH is a documented, deliberate decision, not a silent gap
- [ ] Self-audit passed with all green
- [ ] `docs/`, `phases/phase-roadmap.md`, `CLAUDE.md` updated to match what was actually built
- [ ] Manual verification done by architect

---

## Note for the next v3 phase

The next phase (`RECOLOR`) is the first one that needs real new infrastructure: a mask-conveyance strategy against a provider with no mask parameter, mask-contract validation on ingest, and a generate-then-composite step to guarantee everything outside the masked region is byte-identical to the source. Do not generate that phase file until this one is actually built and verified — per this roadmap's own rule 1, a phase written against a plan rather than a verified MATCH implementation risks assuming fan-out or config-resolution patterns that turned out different in practice.
