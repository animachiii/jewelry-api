# Data Model

**Database:** Supabase Postgres 15. Migrations via Alembic. All timestamps `TIMESTAMPTZ`,
UTC. All primary keys `UUID` with `gen_random_uuid()` default unless noted.

**Connection:** use the Supabase **session pooler** connection string (port 5432) for the
API and workers — these are long-lived processes, not serverless functions. Do not use the
transaction pooler (6543); it does not support prepared statements, which SQLAlchemy uses.

**Row Level Security:** disabled on all tables. The backend connects with the service role
and is the only writer. The Flutter ERP never touches Postgres directly.

---

## Enums

Create as native Postgres enums.

```
angle_t          FRONT | SIDE | DIAGONAL | TOP

job_status_t     PENDING | PROCESSING | COMPLETED | PARTIAL_SUCCESS | FAILED

sub_job_status_t PENDING | MATTING | GENERATING | QA_REVIEW
                 | COMPLETED | FAILED | REJECTED | SKIPPED

source_type_t    UPLOADED | SYNTHETIC

asset_kind_t     INPUT | MATTE | OUTPUT

failure_class_t  TRANSIENT_PROVIDER | TRANSIENT_NETWORK | RATE_LIMITED
                 | INVALID_INPUT | SAFETY_REFUSAL | QA_REJECTED | INTERNAL

qa_status_t      NOT_APPLICABLE | PASSED | FLAGGED | FAILED

sync_status_t    SUCCESS | FAILED
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
    "default_negative_prompt": "..."
  }
}
```

Seven categories total. Exact codes are confirmed during Phase 0 from the client's sheet.

---

## `jobs`

One row per `POST /api/v2/generate`.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | UUID PK | This is the `job_id` returned to the client |
| `client_id` | UUID NOT NULL FK → `api_clients.id` | |
| `idempotency_key` | TEXT NOT NULL | Client-supplied |
| `category_code` | TEXT NOT NULL | Must exist in the active config version |
| `config_version_id` | UUID NOT NULL FK → `config_versions.id` | Pinned at creation. Never changes. |
| `status` | `job_status_t` NOT NULL DEFAULT `PENDING` | |
| `requested_angles` | INT NOT NULL | Count of angles not skipped |
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

One row per angle, including skipped angles.

| Column | Type | Notes |
| :--- | :--- | :--- |
| `id` | UUID PK | |
| `job_id` | UUID NOT NULL FK → `jobs.id` ON DELETE CASCADE | |
| `angle` | `angle_t` NOT NULL | |
| `status` | `sub_job_status_t` NOT NULL DEFAULT `PENDING` | |
| `source_type` | `source_type_t` NOT NULL | `SYNTHETIC` when no input image was supplied |
| `celery_task_id` | TEXT NULL | |
| `input_asset_id` | UUID NULL FK → `assets.id` | NULL for `SYNTHETIC` and `SKIPPED` |
| `matte_asset_id` | UUID NULL FK → `assets.id` | |
| `output_asset_id` | UUID NULL FK → `assets.id` | |
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

Unique: `(job_id, angle)`.
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

Unique: `(bucket, storage_path)`.
Index: `(job_id, kind)`, `(expires_at)` where not null.

### Bucket layout

| Bucket | Public? | Contents | Retention |
| :--- | :--- | :--- | :--- |
| `jewelry-inputs` | Private | Client-uploaded source photos | 90 days (retry window + audit) |
| `jewelry-outputs` | Private | Final generated images | Indefinite until client policy set |

`jewelry-mattes` was deleted 2026-08-07 — see `docs/decisions/0001-drop-local-matting.md`.

Path convention: `{job_id}/{angle}/{kind}_{short_uuid}.{ext}`

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
| `operation` | TEXT NOT NULL | e.g. `image_generation` |
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
| `provider:gemini:tokens` | Global provider token bucket | rolling |
| `celery-*` | Broker and result backend | Celery-managed |

Every one of these is rebuildable. Flushing Redis must not lose client-visible state.
