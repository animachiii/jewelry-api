# Data Model

**Database:** Supabase Postgres 15. Migrations via Alembic. All timestamps `TIMESTAMPTZ`,
UTC. All primary keys `UUID` with `gen_random_uuid()` default unless noted.

**Connection:** use the Supabase **session pooler** connection string (port 5432) for the
API and workers — these are long-lived processes, not serverless functions. Do not use the
transaction pooler (6543); it does not support prepared statements, which SQLAlchemy uses.

**Row Level Security:** disabled on all tables. The backend connects with the service role
and is the only writer. The Flutter ERP never touches Postgres directly.

**Verified 2026-08-15 (Phase 16 Step 3):** no anon-role credential exists in any deployed
environment or client-facing code. `DATABASE_URL` in every environment (checked against
`.env`, the authoritative known-good config — see `docs/deployment-free-tier.md`) uses
`postgres.<project-ref>` on the session pooler (port 5432), the full Postgres role, not an
anon/authenticated JWT role. A repo-wide grep (`ui/index.html` included) for a Supabase
anon key or `SUPABASE_ANON_KEY` returns zero matches. `docs/integration-guide.md` never
instructs the Flutter team to hold a Supabase credential — only presigned upload URLs and
signed output URLs, both scoped, time-limited grants issued by this backend. Live Supabase
advisory flags this as "critical" using generic language ("anyone with the anon key can
read or modify every row") that does not apply here — no anon key is ever distributed to
any client. The Supabase project connection is Postgres session-pooler only, not the REST
API a browser client would use, so this architecture is not what the advisory is written
for.

---

## Enums

Create as native Postgres enums.

```
angle_t          FRONT | SIDE | DIAGONAL | TOP

job_status_t     PENDING | PROCESSING | COMPLETED | PARTIAL_SUCCESS | FAILED

sub_job_status_t PENDING | MATTING | GENERATING | QA_REVIEW
                 | COMPLETED | FAILED | REJECTED | SKIPPED

source_type_t    UPLOADED | SYNTHETIC

asset_kind_t     INPUT | MATTE | OUTPUT | MASK

failure_class_t  TRANSIENT_PROVIDER | TRANSIENT_NETWORK | RATE_LIMITED
                 | INVALID_INPUT | SAFETY_REFUSAL | QA_REJECTED | INTERNAL

qa_status_t      NOT_APPLICABLE | PASSED | FLAGGED | FAILED

sync_status_t    SUCCESS | FAILED

operation_t      ANGLE_GENERATION | BACKGROUND_REMOVAL | BACKGROUND_REPLACEMENT
                 | MATCH | RECOLOR
```

`REJECTED` is distinct from `FAILED`: it means the provider deterministically declined
(safety refusal) or QA rejected the output. Retry is not offered for `REJECTED`.

`SKIPPED` means the client explicitly did not submit this angle. It is not a failure and
does not count toward partial-success math.

`sub_job_status_t.MATTING` and `asset_kind_t.MATTE` are **vestigial** — see
`docs/decisions/0001-drop-local-matting.md`. Local matting was dropped after
this schema landed; removing an enum value requires recreating the Postgres
type, which wasn't worth it for values nothing referenced yet. No code path
sets either value. Do not use them in new code.

`operation_t` (Phase 15, migration 0006) — `jobs.operation` defaults to
`ANGLE_GENERATION`, so every pre-Phase-15 row is unaffected. See
`phases/phase-15-background-operations.md` and `docs/business-rules.md`'s
operations section. `MATCH` was added by migration 0013 (Phase 18) via
`ALTER TYPE ... ADD VALUE` — see that migration's docstring for why this is
the first migration in this project to actually do that (0006 created
`operation_t` fresh with all its values baked in; it did not set an
ADD VALUE precedent despite `phases/phase-18-match.md` claiming it did).
`RECOLOR` was added by migration 0015 (Phase 19), same `ALTER TYPE ... ADD
VALUE` mechanism as `MATCH`. `asset_kind_t.MASK` was added by the same
migration — see `docs/business-rules.md` §15 and
`phases/phase-19-recolor.md` Step 1.

---

## `api_clients`

Machine credentials for the Flutter ERP and any future consumer.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | UUID PK | |
| `name` | TEXT NOT NULL | e.g. "Asish Flutter ERP — production" |
| `key_prefix` | TEXT NOT NULL UNIQUE | First 8 chars of the key, for lookup and logs |
| `key_hash` | TEXT NOT NULL | Argon2 hash of the full key. Raw key never stored. |
| `scope` | TEXT NOT NULL DEFAULT 'client' | `client` \| `ops` — see docs/api-routes.md Auth scopes. Added in migration 0002; missing from the original Phase 0 plan. |
| `rate_limit_per_min` | INT NOT NULL DEFAULT 60 | |
| `daily_job_quota` | INT NULL | NULL = unlimited |
| `is_active` | BOOLEAN NOT NULL DEFAULT TRUE | |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `revoked_at` | TIMESTAMPTZ NULL | |

Index: `key_prefix`.

---

## `config_versions`

Immutable snapshots of the Google Sheets configuration. Never updated after insert —
except `is_active`, which is toggled during activation.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | UUID PK | |
| `version_number` | BIGINT NOT NULL UNIQUE | Monotonic, app-assigned |
| `source_hash` | TEXT NOT NULL | SHA-256 of the normalized Sheets payload. Unchanged hash = no new version. |
| `payload` | JSONB NOT NULL | Full category → angle → prompt/reference matrix |
| `sync_status` | `sync_status_t` NOT NULL | |
| `error_message` | TEXT NULL | Populated when `sync_status = FAILED` |
| `is_active` | BOOLEAN NOT NULL DEFAULT FALSE | Exactly one row TRUE at a time |
| `synced_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `activated_at` | TIMESTAMPTZ NULL | |

Partial unique index: `CREATE UNIQUE INDEX ON config_versions (is_active) WHERE is_active`.

### `payload` shape

```json
{
  "categories": [
    {
      "code": "RING",
      "name": "Rings",
      "is_active": true,
      "angles": {
        "FRONT":    { "enabled": true,  "synthetic_allowed": false, "prompt": "...", "reference_image_urls": ["..."], "negative_prompt": "..." },
        "SIDE":     { "enabled": true,  "synthetic_allowed": false, "prompt": "...", "reference_image_urls": [] },
        "DIAGONAL": { "enabled": true,  "synthetic_allowed": true,  "prompt": "...", "reference_image_urls": ["..."] },
        "TOP":      { "enabled": false, "synthetic_allowed": false, "prompt": null,  "reference_image_urls": [] }
      }
    }
  ],
  "global": {
    "model_version": "gemini-<pinned-version>",
    "qa_similarity_threshold": 0.82,
    "default_negative_prompt": "...",
    "unit_cost_usd": 0.02,
    "background_qa_similarity_threshold": 0.92,
    "operations": {
      "BACKGROUND_REMOVAL": { "enabled": true, "prompt": "...", "unit_cost_usd": 0.02 },
      "BACKGROUND_REPLACEMENT": {
        "enabled": true,
        "unit_cost_usd": 0.02,
        "custom_background_prompt": "..."
      },
      "MATCH": { "enabled": true, "prompt": "...{target_category}...", "unit_cost_usd": 0.02 },
      "RECOLOR": { "enabled": true, "prompt": "...{palette_prompt}...", "unit_cost_usd": 0.02 }
    },
    "background_presets": [
      { "code": "STUDIO_WHITE", "name": "Studio White", "prompt": "...",
        "reference_image_urls": [], "is_active": true }
    ],
    "palette": [
      { "code": "EMERALD_GREEN", "label": "Emerald", "prompt_phrase": "...", "is_active": true }
    ]
  }
}
```

Seven categories total. Exact codes are confirmed during Phase 0 from the client's sheet.

**Sheets -> payload normalization (Phase 3):** `app/services/config_sync_service.py`
builds this shape from raw Sheets rows (`app/providers/sheets.py`) — one row per
category/angle pair plus a `Global` key/value tab. The exact column layout is an
assumed convention, not yet confirmed against a real client sheet (roadmap open
decision #2) — see `app/providers/sheets.py`'s module docstring for the assumed
columns.

**`global.unit_cost_usd` (Phase 6):** added to satisfy docs/business-rules.md
§10 — "`unit_cost_usd` comes from configuration, never a hardcoded constant."
The original payload shape had no cost field at all; this is a placeholder
Gemini image-generation price, to be confirmed against real billing before
launch, same status as `qa_similarity_threshold`.

**`global.operations`, `global.background_presets`, `global.background_qa_similarity_threshold`
(Phase 15, migrations 0007/0010):** live inside `global`, not as top-level `payload`
keys, deliberately — `config_sync_service.normalize_sheet_rows` rebuilds `categories`
from Sheets rows on every sync but only ever carries the `global` block forward
untouched (the same mechanism that already protects `unit_cost_usd`). The real Sheet
has no Global tab, so these can only ever be seeded by migration and inherited
forward. All three are placeholders pending real business decisions — see
`docs/decisions/0002-background-removal-approach.md` and roadmap open decision #11
(preset list).

**`global.operations.MATCH` (Phase 18, migration 0014):** seeded the same way,
alongside the two `BACKGROUND_*` keys already there rather than replacing them
(migration 0014 merges into the existing `operations` object; a naive
`setdefault` would have silently dropped the background operations' own config
on a database that already ran `0007`). `prompt` carries a genuine runtime
`{target_category}` placeholder, substituted per-request — see
`app/services/job_service.py::resolve_match_prompt` and roadmap open decision
#12 (prompt wording / pricing not yet reviewed by the client).

**`global.operations.RECOLOR` and `global.palette` (Phase 19, migration 0016):**
`operations.RECOLOR` seeded the same merge-not-replace way as `MATCH` (this is the
*third* migration to touch `operations`). `prompt` carries a genuine runtime
`{palette_prompt}` placeholder — see `app/services/job_service.py::resolve_recolor_prompt`.
`global.palette` is a brand-new top-level `global` key (no prior migration to
collide with), a fixed set of client-selectable colors — raw hex input is
deliberately not supported (see `phases/phase-19-recolor.md`'s reality-check
section). Same uncalibrated-placeholder status as every other seeded
prompt/cost/palette entry in this project.

---

## `jobs`

One row per `POST /api/v2/generate`, `POST /api/v2/background/remove`, or
`POST /api/v2/background/replace`.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | UUID PK | This is the `job_id` returned to the client |
| `client_id` | UUID NOT NULL FK → `api_clients.id` | |
| `idempotency_key` | TEXT NOT NULL | Client-supplied |
| `payload_hash` | TEXT NOT NULL | SHA-256 of the normalized request body. Added in migration 0003 — durable version of the same-key/different-payload 409 check in docs/business-rules.md §8; a Redis-only hash doesn't survive the 24h TTL. |
| `category_code` | TEXT NULL | Must exist in the active config version for an `ANGLE_GENERATION` job. **NULL for a background-operation job** — there is no category (migration 0008, Phase 15). |
| `operation` | `operation_t` NOT NULL DEFAULT `ANGLE_GENERATION` | Added in migration 0006 (Phase 15). |
| `preset_code` | TEXT NULL | Set only for `BACKGROUND_REPLACEMENT` — the pinned backdrop preset the worker resolves its prompt from. Added in migration 0009 (Phase 15). |
| `config_version_id` | UUID NOT NULL FK → `config_versions.id` | Pinned at creation. Never changes. |
| `status` | `job_status_t` NOT NULL DEFAULT `PENDING` | |
| `requested_angles` | INT NOT NULL | Count of angles not skipped for an angle job; always `1` for a background job; count of requested companion-piece variants (1-4) for a `MATCH` job; always `1` for a `RECOLOR` job (same posture as a background job — exactly one sub-job). Keeps its name across all four — see docs/business-rules.md. |
| `succeeded_angles` | INT NOT NULL DEFAULT 0 | |
| `failed_angles` | INT NOT NULL DEFAULT 0 | |
| `sku_reference` | TEXT NULL | Client's own product reference, passed through |
| `metadata` | JSONB NOT NULL DEFAULT '{}' | Opaque client passthrough |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `started_at` | TIMESTAMPTZ NULL | |
| `completed_at` | TIMESTAMPTZ NULL | |

Unique: `(client_id, idempotency_key)`.
Indexes: `(client_id, created_at DESC)`, `(status)` partial where status in
(`PENDING`,`PROCESSING`).

---

## `sub_jobs`

One row per angle (including skipped angles) for an `ANGLE_GENERATION` job; exactly
one row, `angle IS NULL`, for a background-operation job; 1-4 rows, `angle IS NULL`
and `variant_index` 0-based and distinct, for a `MATCH` job (migration 0013,
Phase 18); exactly one row, `angle IS NULL`, `mask_asset_id` and `palette_code` set,
for a `RECOLOR` job (migration 0015, Phase 19) — same single-sub-job shape as a
background-operation job.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | UUID PK | |
| `job_id` | UUID NOT NULL FK → `jobs.id` ON DELETE CASCADE | |
| `angle` | `angle_t` NULL | **NULL for a background-operation job** (migration 0006, Phase 15) — see the two partial indexes below and the operation/angle invariant in `app/services/job_service.py::validate_operation_angle_consistency` (the cross-table CHECK Postgres can't express). |
| `status` | `sub_job_status_t` NOT NULL DEFAULT `PENDING` | |
| `source_type` | `source_type_t` NOT NULL | `SYNTHETIC` when no input image was supplied |
| `celery_task_id` | TEXT NULL | |
| `input_asset_id` | UUID NULL FK → `assets.id` | NULL for `SYNTHETIC` and `SKIPPED` |
| `matte_asset_id` | UUID NULL FK → `assets.id` | |
| `output_asset_id` | UUID NULL FK → `assets.id` | |
| `background_asset_id` | UUID NULL FK → `assets.id` | Set only for a BACKGROUND_REPLACEMENT sub-job that used an uploaded background photo instead of a preset (migration 0011). An ordinary INPUT-kind asset, distinguished from `input_asset_id` only by which FK column points at it. |
| `variant_index` | INT NULL | Set only for a `MATCH` sub-job — its 0-based position among the job's requested companion-piece variants (migration 0013, Phase 18). NULL for every other operation, mirroring how `angle` is NULL for non-`ANGLE_GENERATION` operations. |
| `mask_asset_id` | UUID NULL FK → `assets.id` | Set only for a `RECOLOR` sub-job — the uploaded `MASK`-kind asset consumed server-side to build the Gemini overlay and drive generate-then-composite (migration 0015, Phase 19). Never sent to the provider directly. |
| `palette_code` | TEXT NULL | Set only for a `RECOLOR` sub-job — the requested target color, validated against `payload.global.palette` (migration 0015, Phase 19). |
| `prompt_snapshot` | TEXT NULL | Exact resolved prompt sent to the provider |
| `model_version` | TEXT NULL | Pinned provider model string actually used |
| `seed` | BIGINT NULL | Recorded for reproducibility |
| `attempt_count` | INT NOT NULL DEFAULT 0 | Increments on manual retry and internal backoff |
| `failure_class` | `failure_class_t` NULL | |
| `error_message` | TEXT NULL | Safe for client display. No stack traces. |
| `qa_score` | NUMERIC(4,3) NULL | Perceptual similarity, 0.000–1.000 |
| `qa_status` | `qa_status_t` NOT NULL DEFAULT `NOT_APPLICABLE` | |
| `started_at` | TIMESTAMPTZ NULL | |
| `completed_at` | TIMESTAMPTZ NULL | |

Three partial unique indexes (the first two from migration 0006, replacing the old
plain `UNIQUE(job_id, angle)` — Postgres treats `NULL` as distinct from every other
`NULL`, so a plain unique constraint would not stop a job accumulating more than one
angle-less sub-job; the third added in migration 0013, Phase 18):
- `ux_sub_jobs_job_angle` — `(job_id, angle)` unique where `angle IS NOT NULL`
- `ux_sub_jobs_job_single` — `(job_id)` unique where `angle IS NULL AND variant_index IS NULL`.
  Narrowed by migration 0013 from plain `angle IS NULL` so a `MATCH` job's several
  angle-less, variant-indexed sub-jobs don't collide with it — background-operation
  sub-jobs have `variant_index` NULL too, so the "exactly one angle-less sub-job per
  job" invariant for `BACKGROUND_REMOVAL`/`BACKGROUND_REPLACEMENT` is unchanged.
- `ux_sub_jobs_job_variant` — `(job_id, variant_index)` unique where `variant_index IS NOT NULL`.
  The `MATCH` equivalent of `ux_sub_jobs_job_angle`, enforcing "no duplicate
  variant_index within a job."

Index: `(job_id)`, `(status)` partial where status = `QA_REVIEW`.

---

## `assets`

Every stored image. Rows are never deleted.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | UUID PK | |
| `job_id` | UUID NOT NULL FK → `jobs.id` | |
| `sub_job_id` | UUID NULL FK → `sub_jobs.id` | NULL for job-level assets |
| `kind` | `asset_kind_t` NOT NULL | |
| `bucket` | TEXT NOT NULL | Supabase Storage bucket name |
| `storage_path` | TEXT NOT NULL | Path within the bucket |
| `mime_type` | TEXT NOT NULL | |
| `width_px` | INT NULL | |
| `height_px` | INT NULL | |
| `bytes` | BIGINT NULL | |
| `checksum_sha256` | TEXT NULL | |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `expires_at` | TIMESTAMPTZ NULL | Retention deadline; storage lifecycle removes bytes |
| `purged_at` | TIMESTAMPTZ NULL | Set by the Phase 4 retention worker (`app/workers/retention.py`) once bytes are actually removed from Storage. `NULL` until then. Added in migration `0004`. The row itself is never deleted — a row with `purged_at` set and no bytes still answers "what did we produce for this SKU." |

Unique: `(bucket, storage_path)`.
Index: `(job_id, kind)`, `(expires_at)` where not null.

### Bucket layout

| Bucket | Public? | Contents | Retention |
| :--- | :--- | :--- | :--- |
| `jewelry-inputs` | Private | Client-uploaded source photos | 90 days (retry window + audit) |
| `jewelry-outputs` | Private | Final generated images | Indefinite until client policy set |

`jewelry-mattes` was deleted 2026-08-07 — see `docs/decisions/0001-drop-local-matting.md`.

Path convention: `{job_id}/{angle}/{kind}_{short_uuid}.{ext}`. A background-operation
output uses the literal segment `background` in place of `{angle}` (Phase 15,
`app/services/background_service.py`).

All client-facing URLs are **signed URLs with a 1-hour TTL**, generated at status-read
time. Never store a signed URL in the database — store the path and sign on read.

---

## `cost_events`

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | UUID PK | |
| `job_id` | UUID NOT NULL FK → `jobs.id` | Denormalized for reporting |
| `sub_job_id` | UUID NULL FK → `sub_jobs.id` | |
| `provider` | TEXT NOT NULL | e.g. `gemini` |
| `operation` | TEXT NOT NULL | `image_generation` for angle jobs; `background_removal` / `background_replacement` for Phase 15 background jobs |
| `model_version` | TEXT NOT NULL | |
| `units` | INT NOT NULL DEFAULT 1 | Images generated |
| `unit_cost_usd` | NUMERIC(10,6) NOT NULL | From config, not hardcoded |
| `total_cost_usd` | NUMERIC(10,6) NOT NULL | |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Index: `(job_id)`, `(created_at)`.

A cost event is written on **every** provider call, including calls that fail after
billing. Failed generations still cost money.

---

## `job_events`

Append-only audit log. Every state transition writes one row.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | BIGSERIAL PK | |
| `job_id` | UUID NOT NULL FK → `jobs.id` | |
| `sub_job_id` | UUID NULL FK → `sub_jobs.id` | |
| `event_type` | TEXT NOT NULL | `JOB_CREATED`, `SUBJOB_STATUS_CHANGE`, `RETRY_REQUESTED`, … |
| `from_status` | TEXT NULL | |
| `to_status` | TEXT NULL | |
| `detail` | JSONB NOT NULL DEFAULT '{}' | |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Index: `(job_id, created_at)`.

---

## Relationships

```
api_clients 1──∞ jobs
config_versions 1──∞ jobs
jobs 1──∞ sub_jobs        (1 to 4)
jobs 1──∞ assets
sub_jobs 1──∞ assets      (input, matte, output)
sub_jobs 1──∞ cost_events
jobs 1──∞ job_events
```

## What lives in Redis (and nowhere else)

| Key pattern | Purpose | TTL |
| :--- | :--- | :--- |
| `config:active` | Serialized active config payload | 15 min |
| `idem:{client_id}:{key}` | Idempotency key → job_id | 24 h |
| `ratelimit:{client_id}:{minute}` | Token bucket counter | 2 min |
| `provider:gemini:tokens:{minute-window}` | Global provider rate-limit counter (Phase 6, `app/services/rate_limiter.py`) — fixed-window, not a true token bucket | 2 min |
| `celery-*` | Broker and result backend | Celery-managed |

Every one of these is rebuildable. Flushing Redis must not lose client-visible state.
