# GENERATE_WITH_CLEANUP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new `POST /api/v2/generate-with-cleanup` endpoint that, from one
uploaded photo, runs a background-cleanup Gemini call and then generates 1-4
catalogue angles from the cleaned output — one API call, one `job_id`, two
internal phases.

**Architecture:** Seventh `operation_t` value. Phase 1 creates one job plus
one angle-less "cleanup" sub-job and dispatches it. Phase 2 — triggered from
the **worker layer**, never the service, after the cleanup call commits
`COMPLETED` — creates N angle sub-jobs pointing at the cleanup's output asset
and dispatches the existing, completely unmodified `generation.transform_photo`
for each. If cleanup fails, the job fails immediately with no angle sub-jobs
ever created; the unmodified `compute_parent_status` handles this correctly
because it counts real rows, not a stored request count.

**Tech Stack:** FastAPI + Pydantic v2, SQLAlchemy 2.0 async, Alembic, Celery
5.4 + Redis, Supabase Postgres + Storage, Gemini via the existing
`GeminiProvider` — no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md`

## Global Constraints

- Operation enum value: `GENERATE_WITH_CLEANUP` (user-confirmed final name).
- Cleanup prompt: reuse `BACKGROUND_REMOVAL`'s existing prompt text verbatim
  (user-confirmed — "yes reuse").
- The cleanup step **never** enters `QA_REVIEW` — straight `GENERATING` ->
  `COMPLETED`/`FAILED`/`REJECTED`, same posture Mode A real-photo angles
  already have.
- The cleanup output is **never** exposed in `GET /status` — no `results`
  entry, no field, nothing. Verifiable only via `GET /jobs`/`job_events`
  (ops-only).
- Angle sub-jobs are created **only after** cleanup reaches `COMPLETED`. Never
  pre-created at request time. This is load-bearing — see spec §4 for the
  three independent bugs eager creation causes.
- `docs/conventions.md`: routes validate + delegate only; services own
  transactions; repositories own every query; workers call services, never
  query directly; only `app/providers/` imports a model SDK.
- `docs/conventions.md`: `ruff check`, `ruff format --check`, `mypy --strict`
  all clean before any commit that isn't marked WIP.
- Every schema change is an Alembic migration with a working `downgrade()`;
  enum values are never removed.
- No live Gemini calls in tests — fixture-driven
  (`tests/fixtures/gemini/success.json` etc.), same as every other operation.

---

### Task 1: `operation_t` enum value + `jobs.requested_angle_codes` column

**Files:**
- Create: `migrations/versions/0021_add_generate_with_cleanup_operation.py`
- Modify: `app/db/models/enums.py:47` (after `MIX = "MIX"`)
- Modify: `app/db/models/jobs.py` (new `Job` column, after `preset_code`)
- Test: `tests/unit/test_generate_with_cleanup_schema.py`

**Interfaces:**
- Produces: `Operation.GENERATE_WITH_CLEANUP` (str enum member); `Job.requested_angle_codes: list[str] | None` (ORM attribute, JSONB column, nullable)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_generate_with_cleanup_schema.py
"""Schema additions for GENERATE_WITH_CLEANUP: the operation_t enum value
and jobs.requested_angle_codes, added so the worker can learn which angles
to build after the cleanup sub-job commits (the request body is long gone
by then) — the same reason migration 0009 added jobs.preset_code. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 3.
"""

from app.db.models.enums import Operation
from app.db.models.jobs import Job


def test_generate_with_cleanup_is_a_valid_operation() -> None:
    assert Operation.GENERATE_WITH_CLEANUP == "GENERATE_WITH_CLEANUP"


def test_job_has_requested_angle_codes_column() -> None:
    assert "requested_angle_codes" in Job.__table__.columns
    column = Job.__table__.columns["requested_angle_codes"]
    assert column.nullable is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_generate_with_cleanup_schema.py -v`
Expected: FAIL — `AttributeError: GENERATE_WITH_CLEANUP` (enum has no such member yet)

- [ ] **Step 3: Add the enum value**

In `app/db/models/enums.py`, in the `Operation` class:

```python
class Operation(enum.StrEnum):
    ANGLE_GENERATION = "ANGLE_GENERATION"
    BACKGROUND_REMOVAL = "BACKGROUND_REMOVAL"
    BACKGROUND_REPLACEMENT = "BACKGROUND_REPLACEMENT"
    MATCH = "MATCH"
    RECOLOR = "RECOLOR"
    MIX = "MIX"
    GENERATE_WITH_CLEANUP = "GENERATE_WITH_CLEANUP"
```

- [ ] **Step 4: Add the `Job.requested_angle_codes` column**

In `app/db/models/jobs.py`, in the `Job` class, immediately after the
`preset_code` field (around line 65):

```python
    # NULL unless operation == GENERATE_WITH_CLEANUP. That operation defers
    # creating its angle sub-jobs until the cleanup step succeeds (see
    # app/workers/cleanup.py), by which point the original request body no
    # longer exists — this durably records which angles to build, the same
    # reason migration 0009 added preset_code for BACKGROUND_REPLACEMENT.
    # See migration 0021 and docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md
    # section 3.
    requested_angle_codes: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True
    )
```

- [ ] **Step 5: Write the migration**

```python
# migrations/versions/0021_add_generate_with_cleanup_operation.py
"""add GENERATE_WITH_CLEANUP operation, jobs.requested_angle_codes

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-31

GENERATE_WITH_CLEANUP is a two-phase pipeline: one uploaded photo is
background-cleaned, then 1-4 catalogue angles are generated from that
cleaned image. See docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md.

Two schema changes, one migration — same "safe to combine" reasoning
0017's own docstring established: on Postgres 12+, `ALTER TYPE ... ADD
VALUE` can run inside a transaction as long as the new value is never
compared or inserted as data within that same transaction, and this
migration's column DDL does neither.

`jobs.requested_angle_codes` is NULL for every operation except
GENERATE_WITH_CLEANUP — mirrors `preset_code`'s own nullability story
(migration 0009). It records which angles were requested so the worker can
create their sub-jobs *after* the cleanup step succeeds, once the original
request body is gone. `jobs.requested_angles` keeps its existing meaning
(a count) for this operation too — this new column is the one place the
actual angle *codes* are durably recorded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE operation_t ADD VALUE IF NOT EXISTS 'GENERATE_WITH_CLEANUP'")
    op.add_column(
        "jobs",
        sa.Column("requested_angle_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "requested_angle_codes")
    # Deliberately no attempt to remove 'GENERATE_WITH_CLEANUP' from
    # operation_t — Postgres has no ALTER TYPE ... DROP VALUE, and enum
    # values are never removed in this project (see migration 0017's own
    # downgrade for the same note).
```

- [ ] **Step 6: Run migration locally**

Run: `alembic upgrade head`
Expected: migration `0021` applies with no errors against the local dev database.

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest tests/unit/test_generate_with_cleanup_schema.py -v`
Expected: PASS

- [ ] **Step 8: Lint and type-check**

Run: `ruff check app/ migrations/ tests/ && ruff format --check app/ migrations/ tests/ && mypy --strict app/`
Expected: all clean

- [ ] **Step 9: Commit**

```bash
git add app/db/models/enums.py app/db/models/jobs.py migrations/versions/0021_add_generate_with_cleanup_operation.py tests/unit/test_generate_with_cleanup_schema.py
git commit -m "feat: add GENERATE_WITH_CLEANUP operation and jobs.requested_angle_codes"
```

---

### Task 2: `validate_operation_angle_consistency` — the third case

**Files:**
- Modify: `app/services/job_service.py:124-135`
- Test: `tests/unit/test_generate_with_cleanup_validation.py`

**Interfaces:**
- Consumes: `Operation` (Task 1), `Angle | None`
- Produces: `validate_operation_angle_consistency(operation: Operation, angle: Angle | None) -> None` — same signature, now with a third branch. Callers unchanged.

This function currently enforces "angle non-null **iff** `ANGLE_GENERATION`".
`GENERATE_WITH_CLEANUP` is the first operation whose sub-jobs are heterogeneous
— its cleanup sub-job has `angle=None`, its angle sub-jobs have `angle` set —
so both existing branches must exempt it.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_generate_with_cleanup_validation.py
"""validate_operation_angle_consistency must accept BOTH shapes for
GENERATE_WITH_CLEANUP (angle-less cleanup sub-job, angled sub-jobs) while
every other operation keeps its existing single-shape rule. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 3.
"""

import pytest

from app.db.models.enums import Angle, Operation
from app.services.job_service import validate_operation_angle_consistency


def test_generate_with_cleanup_accepts_angle_none() -> None:
    validate_operation_angle_consistency(Operation.GENERATE_WITH_CLEANUP, None)


def test_generate_with_cleanup_accepts_an_angle() -> None:
    validate_operation_angle_consistency(Operation.GENERATE_WITH_CLEANUP, Angle.FRONT)


def test_angle_generation_still_requires_an_angle() -> None:
    with pytest.raises(ValueError, match="must specify an angle"):
        validate_operation_angle_consistency(Operation.ANGLE_GENERATION, None)


def test_background_removal_still_rejects_an_angle() -> None:
    with pytest.raises(ValueError, match="must not specify an angle"):
        validate_operation_angle_consistency(Operation.BACKGROUND_REMOVAL, Angle.FRONT)


def test_mix_still_rejects_an_angle() -> None:
    with pytest.raises(ValueError, match="must not specify an angle"):
        validate_operation_angle_consistency(Operation.MIX, Angle.SIDE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_generate_with_cleanup_validation.py -v`
Expected: FAIL — `test_generate_with_cleanup_accepts_an_angle` raises
`ValueError: GENERATE_WITH_CLEANUP sub-jobs must not specify an angle`

- [ ] **Step 3: Add the third branch**

Replace `app/services/job_service.py:124-135`:

```python
def validate_operation_angle_consistency(operation: Operation, angle: Angle | None) -> None:
    """The cross-table CHECK Postgres can't express (angle non-null iff the
    parent job is ANGLE_GENERATION — see docs/schema.md and
    phases/phase-15-background-operations.md Step 2). Every code path that
    builds a SubJob must call this before `jobs_repo.create_sub_job`.

    GENERATE_WITH_CLEANUP is exempt from both directions (2026-08-31,
    docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md
    section 3) — it is the first operation with heterogeneous sub-job
    shapes: one angle-less cleanup sub-job, and 1-4 angled sub-jobs created
    once cleanup succeeds. Either angle value is valid for it.
    """
    if operation == Operation.GENERATE_WITH_CLEANUP:
        return
    if operation == Operation.ANGLE_GENERATION and angle is None:
        raise ValueError(f"{operation.value} sub-jobs must specify an angle.")
    if operation != Operation.ANGLE_GENERATION and angle is not None:
        raise ValueError(
            f"{operation.value} sub-jobs must not specify an angle (got {angle.value})."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_generate_with_cleanup_validation.py -v`
Expected: PASS, all 5 tests

- [ ] **Step 5: Run the full existing job_service unit suite to confirm no regression**

Run: `pytest tests/unit/ -k job_service -v`
Expected: PASS

- [ ] **Step 6: Lint and commit**

```bash
ruff check app/ tests/ && ruff format --check app/ tests/ && mypy --strict app/
git add app/services/job_service.py tests/unit/test_generate_with_cleanup_validation.py
git commit -m "feat: exempt GENERATE_WITH_CLEANUP from the single-shape angle invariant"
```

---

### Task 3: `jobs_repo.create_job` — accept `requested_angle_codes`

**Files:**
- Modify: `app/db/repositories/jobs.py:161-197`
- Test: `tests/unit/test_generate_with_cleanup_schema.py` (extend Task 1's file)

**Interfaces:**
- Consumes: `Job` model's new column (Task 1)
- Produces: `create_job(..., requested_angle_codes: list[str] | None = None) -> Job` — new optional kwarg, every existing call site unaffected by the default.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_generate_with_cleanup_schema.py`:

```python
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import jobs as jobs_repo


@pytest.mark.asyncio
async def test_create_job_accepts_requested_angle_codes(db_session: AsyncSession) -> None:
    from app.db.models.config_versions import ConfigVersion
    from app.db.models.enums import SyncStatus
    from datetime import UTC, datetime

    cv = ConfigVersion(
        version_number=999999,
        source_hash="test-hash-requested-angle-codes",
        payload={"global": {"model_version": "test"}},
        sync_status=SyncStatus.SUCCESS,
        is_active=False,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.flush()

    job = jobs_repo.create_job(
        db_session,
        client_id=uuid.uuid4(),
        idempotency_key="test-key",
        payload_hash="test-hash",
        config_version_id=cv.id,
        requested_angles=2,
        sku_reference=None,
        metadata={},
        operation=Operation.GENERATE_WITH_CLEANUP,
        requested_angle_codes=["FRONT", "SIDE"],
    )
    assert job.requested_angle_codes == ["FRONT", "SIDE"]
```

Add `from app.db.models.enums import Operation` to the file's imports if not already present.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_generate_with_cleanup_schema.py::test_create_job_accepts_requested_angle_codes -v`
Expected: FAIL — `TypeError: create_job() got an unexpected keyword argument 'requested_angle_codes'`

- [ ] **Step 3: Add the parameter**

In `app/db/repositories/jobs.py`, modify `create_job`:

```python
def create_job(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    idempotency_key: str,
    payload_hash: str,
    config_version_id: uuid.UUID,
    requested_angles: int,
    sku_reference: str | None,
    metadata: dict[str, Any],
    category_code: str | None = None,
    preset_code: str | None = None,
    operation: Operation = Operation.ANGLE_GENERATION,
    requested_angle_codes: list[str] | None = None,
) -> Job:
    """Adds and returns a new Job row (status defaults to PENDING — nothing
    executes here, see phases/phase-2-data-model.md). Does not commit; the
    caller controls the transaction boundary.

    `operation` defaults to ANGLE_GENERATION so every existing `/generate`
    call site is unaffected — see migration 0006 and
    phases/phase-15-background-operations.md Step 2.

    `requested_angle_codes` is set only for GENERATE_WITH_CLEANUP — see
    migration 0021 and docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md
    section 3.
    """
    job = Job(
        client_id=client_id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=category_code,
        preset_code=preset_code,
        config_version_id=config_version_id,
        status=JobStatus.PENDING,
        operation=operation,
        requested_angles=requested_angles,
        sku_reference=sku_reference,
        job_metadata=metadata,
        requested_angle_codes=requested_angle_codes,
    )
    session.add(job)
    return job
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_generate_with_cleanup_schema.py -v`
Expected: PASS, all tests in the file

- [ ] **Step 5: Run full jobs repository unit tests to confirm no regression**

Run: `pytest tests/unit/ -k "jobs_repo or repositories" -v`
Expected: PASS

- [ ] **Step 6: Lint and commit**

```bash
ruff check app/ tests/ && ruff format --check app/ tests/ && mypy --strict app/
git add app/db/repositories/jobs.py tests/unit/test_generate_with_cleanup_schema.py
git commit -m "feat: create_job accepts requested_angle_codes"
```

---

### Task 4: Seed `operations.GENERATE_WITH_CLEANUP` config

**Files:**
- Create: `migrations/versions/0022_add_generate_with_cleanup_config.py`
- Modify: `scripts/seed_dev.py` (add to `CATEGORY_PAYLOAD["global"]["operations"]`, mirroring how MIX/RECOLOR/MATCH are already seeded there)

**Interfaces:**
- Produces: `config_version.payload["global"]["operations"]["GENERATE_WITH_CLEANUP"] = {"enabled": bool, "prompt": str, "unit_cost_usd": float}`

- [ ] **Step 1: Confirm which `BACKGROUND_REMOVAL` prompt is actually live**

Migration `0007` originally seeded `operations.BACKGROUND_REMOVAL.prompt`,
but migration `0019` (2026-08-18) **superseded** it with a longer prompt
that additionally strips hands/mannequins/tags/props — `0007`'s original
text is stale and must not be copied. The current live text is `0019`'s
`NEW_REMOVAL_PROMPT` (verified against the live database this same session
— it matches exactly):

```
Isolate only the jewellery product and remove everything else from the
frame — hands, fingers, mannequins, models, price tags, hangtags,
stickers, packaging, props, and any other object. Replace the
background with a clean, seamless pure white (#FFFFFF) studio backdrop.
Keep the jewellery itself — its proportions, materials, textures, and
every detail — exactly unchanged. Do not redesign, embellish, or
invent any part of the product. The final image must contain only the
jewellery product on a plain white background, ready to use directly
as an e-commerce product photo.
```

This is the string Step 2 below uses. Before running the migration in a
different environment, re-verify it is still the active prompt (a later
sync could have changed it again) with:
`SELECT payload->'global'->'operations'->'BACKGROUND_REMOVAL'->>'prompt' FROM config_versions WHERE is_active;`

- [ ] **Step 2: Write the migration**

```python
# migrations/versions/0022_add_generate_with_cleanup_config.py
"""feat: seed GENERATE_WITH_CLEANUP into config_versions.payload.global.operations

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-31

GENERATE_WITH_CLEANUP needs the same per-operation enabled/prompt/unit_cost_usd
shape 0007/0014/0016/0018 already seeded for the other operations that use
it. This is the fifth migration to touch payload.global.operations, so it
reads whatever is already there and merges GENERATE_WITH_CLEANUP in — same
merge-not-replace reasoning every prior operations-touching migration's own
docstring already established.

The prompt text is BACKGROUND_REMOVAL's own prompt, copied verbatim — a
deliberate decision, not a placeholder oversight: the cleanup step performs
the exact same transformation standalone background removal does, just as
an internal pipeline stage rather than a client deliverable. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 9.

Same uncalibrated-placeholder cost status as every other seeded operation
in this project.
"""

import hashlib
import json
import ssl
from collections.abc import Sequence
from urllib.parse import urlparse

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

NEW_OPERATIONS = {
    "GENERATE_WITH_CLEANUP": {
        "enabled": True,
        # Copied verbatim from operations.BACKGROUND_REMOVAL.prompt as
        # superseded by migration 0019 (NOT 0007's original, which is
        # stale) -- see this migration's own docstring for why.
        "prompt": (
            "Isolate only the jewellery product and remove everything else from the "
            "frame — hands, fingers, mannequins, models, price tags, hangtags, "
            "stickers, packaging, props, and any other object. Replace the "
            "background with a clean, seamless pure white (#FFFFFF) studio backdrop. "
            "Keep the jewellery itself — its proportions, materials, textures, and "
            "every detail — exactly unchanged. Do not redesign, embellish, or "
            "invent any part of the product. The final image must contain only the "
            "jewellery product on a plain white background, ready to use directly "
            "as an e-commerce product photo."
        ),
        "unit_cost_usd": 0.02,
    }
}


def upgrade() -> None:
    bind = op.get_bind()

    active = (
        bind.execute(
            sa.text(
                "SELECT id, version_number, payload::text AS payload_text "
                "FROM config_versions WHERE is_active = true"
            )
        )
        .mappings()
        .first()
    )

    if active is None:
        return  # fresh/CI database, no seeded config -- nothing to extend

    payload = json.loads(active["payload_text"])
    global_block = dict(payload.get("global", {}))
    operations = dict(global_block.get("operations", {}))
    if "GENERATE_WITH_CLEANUP" in operations:
        return  # already extended

    operations.update(NEW_OPERATIONS)
    global_block["operations"] = operations
    new_payload = dict(payload)
    new_payload["global"] = global_block

    new_hash = hashlib.sha256(json.dumps(new_payload, sort_keys=True).encode()).hexdigest()

    next_version = bind.execute(
        sa.text("SELECT COALESCE(MAX(version_number), 0) + 1 FROM config_versions")
    ).scalar_one()

    bind.execute(
        sa.text("UPDATE config_versions SET is_active = false WHERE id = :id"),
        {"id": active["id"]},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO config_versions
                (id, version_number, source_hash, payload, sync_status,
                 is_active, synced_at, activated_at)
            VALUES (gen_random_uuid(), :version_number, :source_hash,
                    CAST(:payload AS jsonb), 'SUCCESS', true, now(), now())
            """
        ),
        {
            "version_number": next_version,
            "source_hash": new_hash,
            "payload": json.dumps(new_payload),
        },
    )

    _invalidate_config_cache()


def downgrade() -> None:
    bind = op.get_bind()

    active = (
        bind.execute(
            sa.text(
                "SELECT id, version_number, payload::text AS payload_text "
                "FROM config_versions WHERE is_active = true"
            )
        )
        .mappings()
        .first()
    )

    if active is None:
        return

    payload = json.loads(active["payload_text"])
    global_block = payload.get("global", {})
    if global_block.get("operations", {}).get("GENERATE_WITH_CLEANUP") != NEW_OPERATIONS[
        "GENERATE_WITH_CLEANUP"
    ]:
        return  # active row wasn't the one this migration activated

    previous = (
        bind.execute(
            sa.text("SELECT id FROM config_versions WHERE version_number = :v"),
            {"v": active["version_number"] - 1},
        )
        .mappings()
        .first()
    )

    if previous is None:
        return

    bind.execute(
        sa.text("UPDATE config_versions SET is_active = false WHERE id = :id"),
        {"id": active["id"]},
    )
    bind.execute(
        sa.text("UPDATE config_versions SET is_active = true WHERE id = :id"),
        {"id": previous["id"]},
    )

    _invalidate_config_cache()


def _invalidate_config_cache() -> None:
    """Best-effort: a stale cache self-heals within the 15 min TTL
    (app/services/config_service.py), so a Redis hiccup here must not fail
    the migration step of a deploy."""
    try:
        import redis

        ssl_kwargs = (
            {"ssl_cert_reqs": ssl.CERT_REQUIRED}
            if urlparse(settings.REDIS_URL).scheme == "rediss"
            else {}
        )
        client = redis.from_url(settings.REDIS_URL, **ssl_kwargs)
        try:
            client.delete("config:active")
        finally:
            client.close()
    except Exception:  # noqa: BLE001 - never fail a migration over cache invalidation
        pass
```

**Do not leave the placeholder string in `NEW_OPERATIONS["GENERATE_WITH_CLEANUP"]["prompt"]`.** Replace `"<PASTE THE EXACT STRING FOUND IN STEP 1 HERE>"` with the real prompt text found in Step 1 before running this migration.

- [ ] **Step 3: Run migration locally**

Run: `alembic upgrade head`
Expected: migration `0022` applies cleanly

- [ ] **Step 4: Add to `scripts/seed_dev.py`**

Find the `CATEGORY_PAYLOAD["global"]["operations"]` dict in `scripts/seed_dev.py`
and add an entry matching migration `0022`'s `NEW_OPERATIONS["GENERATE_WITH_CLEANUP"]`
exactly (same prompt string, same `enabled`/`unit_cost_usd`) — this mirrors
how `MIX`/`RECOLOR`/`MATCH` are already both migration-seeded (production)
and dev-script-seeded (so a fresh dev/test DB has usable data too).

- [ ] **Step 5: Verify the dev seed still runs**

Run: `python scripts/seed_dev.py`
Expected: completes with no errors; re-running it is idempotent (existing
script behavior, unaffected by this addition)

- [ ] **Step 6: Lint and commit**

```bash
ruff check migrations/ scripts/ && ruff format --check migrations/ scripts/
git add migrations/versions/0022_add_generate_with_cleanup_config.py scripts/seed_dev.py
git commit -m "feat: seed GENERATE_WITH_CLEANUP operation config"
```

---

### Task 5: `cleanup_service.py` — the cleanup Gemini call, no QA gate

**Files:**
- Create: `app/services/cleanup_service.py`
- Test: covered by Task 8's integration tests (matches `background_service.py`'s own precedent — no dedicated unit test file exists for it either, since it has no Pillow/pure-logic surface to unit test in isolation)

**Interfaces:**
- Consumes: `resolve_operation_unit_cost`, `find_operation_config` (existing, `app/services/job_service.py`), `GeminiProvider` (existing, `app/providers/gemini.py`), `acquire_rate_limit` (existing, `app/services/rate_limiter.py`), `recompute_parent_status` (existing, `app/services/generation_service.py`)
- Produces: `async def process(session: AsyncSession, redis_client: Redis, sub_job_id: uuid.UUID) -> SubJob` — same signature shape as `background_service.process`/`mix_service.process`. Sets `sub_job.status` to `COMPLETED` (not `QA_REVIEW`) on success, `FAILED`/`REJECTED` on failure. On success, `sub_job.output_asset_id` points at a new `OUTPUT`-kind asset in `BUCKET_OUTPUTS`. **Callers must check `sub_job.status == SubJobStatus.COMPLETED` after calling this** — that is the trigger for Task 6's phase-2 dispatch.

This is `background_service.py` with the QA-gate branch removed. Write it as
a new, standalone module — do not import from `background_service.py` (this
codebase's established precedent: `recolor_service`/`mix_service`/`match_service`
each reimplement rather than share private helpers across service modules).

- [ ] **Step 1: Write the module**

```python
# app/services/cleanup_service.py
"""Runs the cleanup phase of a GENERATE_WITH_CLEANUP sub-job — the first of
its two phases. See docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md.

Mirrors app/services/background_service.py::process almost exactly (same
rate-limit -> provider call -> cost event -> success/fail shape, same
Gemini call, even the same prompt at rest — this migration 0022 seeds it
as BACKGROUND_REMOVAL's own prompt text, copied verbatim). Kept as a
separate module rather than importing background_service's private
helpers, following this codebase's own precedent (recolor_service.py/
mix_service.py/match_service.py each reimplement rather than share).

The one real difference, and the reason this can't just be
background_service with a parameter: success goes straight to COMPLETED,
never QA_REVIEW. A standalone BACKGROUND_REMOVAL sub-job's QA gate exists
because "the cutout *is* the product" (docs/business-rules.md §13) — here
the cleaned photo is never the product; it's consumed internally by the
angle-generation phase this sub-job's caller triggers next. Same posture
Mode A real-photo angles already have (no QA gate at all).

Never dispatches phase 2 itself. `app/workers/cleanup.py` does that, after
this function's caller commits — same "dispatch from the worker layer,
never the service" rule Phase 9 established for qa.score_similarity, so
the next phase's dispatch never races the creating transaction.
"""

import random
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import ProviderError
from app.db.models.enums import AssetKind, FailureClass, JobStatus, Operation, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.db.repositories import assets as assets_repo
from app.db.repositories import config_versions as config_versions_repo
from app.db.repositories import jobs as jobs_repo
from app.providers.base import GenerationResult
from app.providers.gemini import GeminiProvider
from app.services import cost_service, retention_policy, storage_service
from app.services.generation_service import recompute_parent_status
from app.services.job_service import find_operation_config, resolve_operation_unit_cost
from app.services.rate_limiter import acquire as acquire_rate_limit

# Mirrors background_service.py's own MAX_ATTEMPTS/_RETRYABLE_CLASSES rather
# than importing them — same "two independent constants that happen to
# share a value" precedent this codebase already established.
MAX_ATTEMPTS = 3
_RETRYABLE_CLASSES = {
    FailureClass.RATE_LIMITED,
    FailureClass.TRANSIENT_PROVIDER,
    FailureClass.TRANSIENT_NETWORK,
}

_COST_OPERATION_LABEL = "generate_with_cleanup_cleanup_step"


class SubJobNotFoundError(Exception):
    pass


def _resolve_prompt(config_version) -> str:  # noqa: ANN001
    op_config = find_operation_config(config_version, Operation.GENERATE_WITH_CLEANUP) or {}
    return str(op_config.get("prompt", ""))


async def process(session: AsyncSession, redis_client: Redis, sub_job_id: uuid.UUID) -> SubJob:
    sub_job = await jobs_repo.get_sub_job_by_id(session, sub_job_id)
    if sub_job is None:
        raise SubJobNotFoundError(f"SubJob {sub_job_id} not found.")

    job = await jobs_repo.get_by_id(session, sub_job.job_id)
    if job is None:
        raise SubJobNotFoundError(f"Job {sub_job.job_id} for sub-job {sub_job_id} not found.")

    # cleanup.process only ever runs for the one angle-less cleanup sub-job
    # of a GENERATE_WITH_CLEANUP job -- the angle sub-jobs it later creates
    # stay on generation.transform_photo, same as any ordinary angle job.
    assert job.operation == Operation.GENERATE_WITH_CLEANUP
    assert sub_job.angle is None

    if job.status == JobStatus.PENDING:
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(UTC)
    sub_job.status = SubJobStatus.GENERATING
    sub_job.started_at = datetime.now(UTC)
    await session.commit()

    config_version = await config_versions_repo.get_by_id(session, job.config_version_id)
    if config_version is None:
        raise SubJobNotFoundError(f"Pinned config version {job.config_version_id} not found.")

    prompt = _resolve_prompt(config_version)
    model_version = config_version.payload["global"]["model_version"]
    unit_cost_usd = resolve_operation_unit_cost(config_version, job.operation)

    input_asset = (
        await assets_repo.get_by_id(session, sub_job.input_asset_id)
        if sub_job.input_asset_id is not None
        else None
    )
    if input_asset is None:
        raise SubJobNotFoundError(f"No input asset for sub-job {sub_job_id}.")
    reference_images = [
        storage_service.download_bytes(input_asset.bucket, input_asset.storage_path)
    ]

    provider = GeminiProvider(model_version=model_version)
    seed = random.randint(0, 2**31 - 1)

    last_error: ProviderError | None = None
    while sub_job.attempt_count < MAX_ATTEMPTS:
        sub_job.attempt_count += 1

        allowed = await acquire_rate_limit(redis_client)
        if not allowed:
            last_error = ProviderError(
                "Gemini rate limit window exhausted.", failure_class=FailureClass.RATE_LIMITED
            )
            if last_error.failure_class not in _RETRYABLE_CLASSES:
                break
            continue

        try:
            result = provider.generate(prompt, reference_images, seed)
        except ProviderError as exc:
            last_error = exc
            cost_service.record_cost_event(
                session,
                job_id=job.id,
                sub_job_id=sub_job.id,
                provider="gemini",
                operation=_COST_OPERATION_LABEL,
                model_version=model_version,
                unit_cost_usd=unit_cost_usd,
            )
            if last_error.failure_class not in _RETRYABLE_CLASSES:
                break
            continue

        cost_service.record_cost_event(
            session,
            job_id=job.id,
            sub_job_id=sub_job.id,
            provider="gemini",
            operation=_COST_OPERATION_LABEL,
            model_version=result.model_version,
            unit_cost_usd=unit_cost_usd,
        )
        await _complete_success(session, job.id, sub_job, result, prompt, seed)
        await recompute_parent_status(session, job)
        return sub_job

    assert last_error is not None
    _fail(sub_job, last_error, prompt, seed)
    await recompute_parent_status(session, job)
    return sub_job


async def _complete_success(
    session: AsyncSession,
    job_id: uuid.UUID,
    sub_job: SubJob,
    result: GenerationResult,
    prompt: str,
    seed: int,
) -> None:
    ext = "png" if result.mime_type == "image/png" else "jpg"
    assert sub_job.angle is None
    storage_path = storage_service.build_storage_path(job_id, "cleanup", AssetKind.OUTPUT, ext)
    storage_service.upload_bytes(
        settings.BUCKET_OUTPUTS, storage_path, result.image_bytes, result.mime_type
    )
    output_asset = assets_repo.create_asset(
        session,
        job_id=job_id,
        sub_job_id=sub_job.id,
        kind=AssetKind.OUTPUT,
        bucket=settings.BUCKET_OUTPUTS,
        storage_path=storage_path,
        mime_type=result.mime_type,
        bytes_=len(result.image_bytes),
        expires_at=retention_policy.compute_expires_at(AssetKind.OUTPUT),
    )
    await session.flush()

    sub_job.output_asset_id = output_asset.id
    sub_job.prompt_snapshot = prompt
    sub_job.model_version = result.model_version
    sub_job.seed = seed
    # Straight to COMPLETED, never QA_REVIEW -- see this module's docstring.
    sub_job.status = SubJobStatus.COMPLETED


def _fail(sub_job: SubJob, error: ProviderError, prompt: str, seed: int) -> None:
    sub_job.prompt_snapshot = prompt
    sub_job.seed = seed
    sub_job.failure_class = FailureClass(error.failure_class)
    sub_job.error_message = error.message
    sub_job.status = (
        SubJobStatus.REJECTED
        if error.failure_class == FailureClass.SAFETY_REFUSAL
        else SubJobStatus.FAILED
    )
```

- [ ] **Step 2: Type-check the new module in isolation**

Run: `mypy --strict app/services/cleanup_service.py`
Expected: no issues (the `# noqa: ANN001` on `_resolve_prompt`'s `config_version`
parameter matches how the codebase already leaves `ConfigVersion`-typed
helper params loosely annotated in similar service modules — check
`background_service.py::_resolve_prompt`'s own signature style; if it's
fully typed there, type this one fully too with `ConfigVersion` instead of
using `noqa`)

- [ ] **Step 3: Lint and commit**

```bash
ruff check app/services/cleanup_service.py && ruff format --check app/services/cleanup_service.py
git add app/services/cleanup_service.py
git commit -m "feat: add cleanup_service, the cleanup-phase Gemini call with no QA gate"
```

(This task has no standalone test because `process()` needs a real sub-job
row, config version, and storage — it is exercised end-to-end by Task 8's
integration tests, exactly matching how `background_service.process` has
never had a dedicated unit test file either.)

---

### Task 6: `app/workers/cleanup.py` — the two-phase dispatch

**Files:**
- Create: `app/workers/cleanup.py`
- Test: `tests/unit/test_generate_with_cleanup_task_registration.py`

**Interfaces:**
- Consumes: `cleanup_service.process` (Task 5), `generation_service.mark_sub_job_timed_out` (existing), `jobs_repo.create_sub_job`/`create_job`... no, only `create_sub_job` (existing, angle sub-job creation), `validate_operation_angle_consistency` (Task 2)
- Produces: Celery task `cleanup.process`, registered name `cleanup.process`, routed to `io` queue. This is where phase-2 dispatch logic lives — **the single most important file in this plan.**

This is the file that turns "cleanup succeeded" into "N angle sub-jobs now
exist and are dispatched." It runs the cleanup call (mirroring
`app/workers/background.py`'s session-lifecycle wrapper exactly), and after
that call returns with `sub_job.status == COMPLETED`, creates the angle
sub-jobs (reading `job.requested_angle_codes`, Task 1/3) and dispatches
`generation.transform_photo_task` for each — all in a **second**, separate
transaction from the one `cleanup_service.process` already committed. If
cleanup did not reach `COMPLETED` (it failed), this task does nothing further
— `compute_parent_status` already made the job `FAILED` inside
`cleanup_service.process` itself.

- [ ] **Step 1: Write the failing registration test**

```python
# tests/unit/test_generate_with_cleanup_task_registration.py
"""Mirrors tests/unit/test_background_task_registration.py — cleanup.process
must be registered with Celery and routed to the io queue, or a real
worker process started separately from the test suite will never
recognize the task (this exact class of gap bit MATCH/RECOLOR once
already — see docs/schema.md's note on migration 0016's self-audit).
"""

from app.workers import cleanup as cleanup_worker
from app.workers.celery_app import celery_app


def test_cleanup_process_registered() -> None:
    assert "cleanup.process" in celery_app.tasks


def test_cleanup_process_routed_to_io_queue() -> None:
    routes = celery_app.conf.task_routes
    assert routes["cleanup.*"]["queue"] == "io"


def test_cleanup_process_task_callable_is_the_registered_task() -> None:
    assert cleanup_worker.process_task.name == "cleanup.process"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_generate_with_cleanup_task_registration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.workers.cleanup'`

- [ ] **Step 3: Write the worker module**

```python
# app/workers/cleanup.py
"""Celery task: `cleanup.process`. Session/transaction lifecycle for phase 1
(the cleanup Gemini call, app/services/cleanup_service.py) AND the
phase-1-to-phase-2 handoff for GENERATE_WITH_CLEANUP jobs. See
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md section 4.

Builds its own engine per call from settings.DATABASE_URL (read live) and
its own owned Redis client per call, closed in `finally` — the same
cross-loop reasoning as every other worker task since Phase 7.

**The phase-2 dispatch below is the reason this file exists rather than
just calling cleanup_service.process from somewhere else.** If the cleanup
sub-job reached COMPLETED, this creates the job's angle sub-jobs (reading
Job.requested_angle_codes, since the original request body is long gone by
now) and dispatches generation.transform_photo_task for each — mirroring
exactly how app/workers/generation.py dispatches qa.score_similarity right
after a QA_REVIEW-landing transform_photo commits: from the WORKER layer,
never the service, so the next phase never reads a row before its own
creating transaction has landed. This is a SEPARATE transaction from the
one cleanup_service.process already committed inside `_run`.

If cleanup did not reach COMPLETED (FAILED/REJECTED), nothing further
happens here — compute_parent_status already made the job FAILED inside
cleanup_service.process itself (it counts real rows; with only the one
FAILED cleanup sub-job existing, F == R == 1 falls out of the unmodified
rollup with no special-casing). No angle sub-jobs are ever created for a
job whose cleanup step failed.

Phase 16 Step 1: bounded by settings.WORKER_TASK_TIMEOUT_SECONDS via
asyncio.wait_for, same as every other worker task.
"""

import asyncio
import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.redis_client import new_redis_client
from app.db.models.enums import Angle, SourceType, SubJobStatus
from app.db.models.jobs import Job
from app.db.repositories import jobs as jobs_repo
from app.services.cleanup_service import process
from app.services.generation_service import mark_sub_job_timed_out
from app.services.job_service import validate_operation_angle_consistency
from app.workers._async_utils import run_async
from app.workers.celery_app import celery_app


async def _run(sub_job_id: str) -> tuple[str, str]:
    """Returns (sub_job_status, job_id) — the caller needs job_id to
    trigger phase 2 without a second DB round-trip inside this function's
    own session, which has already closed by the time the caller runs.
    """
    engine = create_async_engine(settings.DATABASE_URL)
    redis_client = new_redis_client()
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job = await process(session, redis_client, uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value, str(sub_job.job_id)
    finally:
        await redis_client.aclose()
        await engine.dispose()


async def _run_timed_out(sub_job_id: str) -> tuple[str, str]:
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            sub_job = await mark_sub_job_timed_out(session, uuid.UUID(sub_job_id))
            await session.commit()
            return sub_job.status.value, str(sub_job.job_id)
    finally:
        await engine.dispose()


async def _dispatch_angle_phase(job_id: str) -> None:
    """Phase 2: create and dispatch the job's angle sub-jobs. Runs in its
    own fresh engine/session, separate from `_run`'s — by the time this is
    called, `_run`'s session has already closed and committed.
    """
    engine = create_async_engine(settings.DATABASE_URL)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            job = await jobs_repo.get_by_id(session, uuid.UUID(job_id))
            assert job is not None, f"Job {job_id} vanished between phase 1 and phase 2"
            assert job.requested_angle_codes, (
                f"Job {job_id} has no requested_angle_codes -- cannot start phase 2"
            )

            cleanup_sub_job = await _get_cleanup_sub_job(session, job.id)
            assert cleanup_sub_job is not None
            assert cleanup_sub_job.output_asset_id is not None

            angle_sub_job_ids: list[uuid.UUID] = []
            for code in job.requested_angle_codes:
                angle = Angle(code)
                validate_operation_angle_consistency(job.operation, angle)
                angle_sub_job = jobs_repo.create_sub_job(
                    session,
                    job_id=job.id,
                    angle=angle,
                    status=SubJobStatus.PENDING,
                    source_type=SourceType.UPLOADED,
                    input_asset_id=cleanup_sub_job.output_asset_id,
                )
                await session.flush()  # assigns angle_sub_job.id
                angle_sub_job_ids.append(angle_sub_job.id)

            await session.commit()
    finally:
        await engine.dispose()

    # Dispatched only after the creating transaction has committed -- same
    # dispatch-after-commit rule this module's own docstring cites.
    from app.workers.generation import transform_photo_task

    for angle_sub_job_id in angle_sub_job_ids:
        transform_photo_task.delay(str(angle_sub_job_id))


async def _get_cleanup_sub_job(session: AsyncSession, job_id: uuid.UUID):  # noqa: ANN201
    result = await session.execute(
        select(Job).where(Job.id == job_id)
    )  # placeholder replaced below
    raise NotImplementedError
```

**Stop — the `_get_cleanup_sub_job` stub above is wrong and must not be
copied as-is.** Replace it with a real query against `SubJob`, not `Job`:

```python
from app.db.models.jobs import SubJob


async def _get_cleanup_sub_job(session: AsyncSession, job_id: uuid.UUID) -> SubJob | None:
    """The job's one angle-less sub-job -- ux_sub_jobs_job_single guarantees
    at most one exists (angle IS NULL AND variant_index IS NULL)."""
    result = await session.execute(
        select(SubJob).where(SubJob.job_id == job_id, SubJob.angle.is_(None))
    )
    return result.scalar_one_or_none()
```

Remove the broken inline stub from Step 3's listing and use this version
instead. Also remove the now-unused `from app.db.models.jobs import Job`
import if `Job` is used only for the type check above — it is still needed
for `jobs_repo.get_by_id`'s return type in `_dispatch_angle_phase`, so keep
it, but do add `from app.db.models.jobs import SubJob` alongside it.

Finish the file with the Celery task itself:

```python
@celery_app.task(name="cleanup.process")  # type: ignore[untyped-decorator]
def process_task(sub_job_id: str) -> str:
    try:
        status, job_id = run_async(
            asyncio.wait_for(_run(sub_job_id), timeout=settings.WORKER_TASK_TIMEOUT_SECONDS)
        )
    except (TimeoutError, SoftTimeLimitExceeded):
        status, job_id = run_async(_run_timed_out(sub_job_id))

    if status == SubJobStatus.COMPLETED.value:
        run_async(_dispatch_angle_phase(job_id))

    return status
```

- [ ] **Step 4: Register the task in `celery_app.py`**

In `app/workers/celery_app.py`, add `"app.workers.cleanup"` to the `include`
list (alongside `"app.workers.mix"`) and add `"cleanup.*": {"queue": "io"}`
to `task_routes` (alongside `"mix.*": {"queue": "io"}`).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_generate_with_cleanup_task_registration.py -v`
Expected: PASS, all 3 tests

- [ ] **Step 6: Lint and type-check**

Run: `ruff check app/workers/cleanup.py app/workers/celery_app.py && ruff format --check app/workers/cleanup.py app/workers/celery_app.py && mypy --strict app/workers/cleanup.py`
Expected: clean. Pay attention to `_get_cleanup_sub_job`'s return type
annotation — it must be `SubJob | None`, not left implicit, or `mypy --strict`
will reject it.

- [ ] **Step 7: Commit**

```bash
git add app/workers/cleanup.py app/workers/celery_app.py tests/unit/test_generate_with_cleanup_task_registration.py
git commit -m "feat: add cleanup.process worker — phase 1 call plus phase-2 angle dispatch"
```

---

### Task 7: Request schema + `job_service.create_generate_with_cleanup_job_for_request`

**Files:**
- Create: `app/api/v2/schemas/generate_with_cleanup.py`
- Modify: `app/services/job_service.py` (new function, alongside `create_mix_job_for_request`)
- Test: `tests/unit/test_generate_with_cleanup_schema.py` (extend)

**Interfaces:**
- Consumes: `find_category`, `validate_operation_enabled`, `_enforce_rate_limit_and_quota`, `IdempotencyKeyConflictError`, `CategoryNotFoundError`, `CategoryInactiveError`, `AngleNotEnabledError`, `NoAnglesRequestedError`, `AssetNotFoundError`, `AssetNotOwnedError`, `ValidationError` (all existing, `app/services/job_service.py` / `app/core/errors.py`), `image_validation.inspect_and_validate` (existing), `retention_policy.compute_expires_at` (existing), `assets_repo.create_asset`, `jobs_repo.create_job` (Task 3), `jobs_repo.create_sub_job`, `job_events_repo.record_event` (all existing)
- Produces: `GenerateWithCleanupRequest` (Pydantic model); `async def create_generate_with_cleanup_job_for_request(session, client, config_version, body, idempotency_key, payload_hash) -> JobAcceptedResponse`

- [ ] **Step 1: Write the request schema**

```python
# app/api/v2/schemas/generate_with_cleanup.py
"""POST /api/v2/generate-with-cleanup.

See docs/api-routes.md and docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md
section 2. Unlike GenerateJobRequest's per-angle dict (each angle can carry
its own storage_path, or be synthetic, or be skipped), `angles` here is a
plain list of angle codes with no per-angle choice -- every angle in this
operation derives from the ONE cleaned photo, so there is nothing per-angle
to specify. Mixing in synthetic angles is deliberately not supported in v1
(a client wanting that already has /generate) -- see the design spec's
section 9.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.enums import Angle


class GenerateWithCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_path: str
    category_code: str
    angles: list[Angle]
    sku_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 2: Write the failing unit test for the job-creation function's validation**

Append to `tests/unit/test_generate_with_cleanup_schema.py`:

```python
def test_generate_with_cleanup_request_rejects_empty_angles_list_at_service_layer() -> None:
    """Not a Pydantic-level constraint on purpose -- every existing /generate-
    family validation failure uses a specific ErrorCode via the standard
    error envelope (docs/conventions.md), not a generic Pydantic 422. See
    NoAnglesRequestedError, already used by /generate for the identical rule.
    """
    from app.api.v2.schemas.generate_with_cleanup import GenerateWithCleanupRequest

    # An empty list is valid Pydantic input -- the check belongs to the
    # service layer (job_service.create_generate_with_cleanup_job_for_request),
    # exercised in the integration tests (Task 8), not here.
    request = GenerateWithCleanupRequest(
        storage_path="pending/test/x/y.jpg", category_code="RING", angles=[]
    )
    assert request.angles == []
```

- [ ] **Step 3: Run test to verify it passes (schema-only, no service logic yet)**

Run: `pytest tests/unit/test_generate_with_cleanup_schema.py -v`
Expected: PASS (this test only proves the schema accepts an empty list; the
service-layer rejection is proven in Task 8)

- [ ] **Step 4: Write `create_generate_with_cleanup_job_for_request`**

Add to `app/services/job_service.py`, near `create_mix_job_for_request`:

```python
async def create_generate_with_cleanup_job_for_request(
    session: AsyncSession,
    client: ApiClient,
    config_version: ConfigVersion,
    body: GenerateWithCleanupRequest,
    idempotency_key: str,
    payload_hash: str,
) -> JobAcceptedResponse:
    """Implements POST /generate-with-cleanup. Creates the job plus its ONE
    cleanup sub-job and dispatches cleanup.process — see
    docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md.

    Unlike create_job_for_request, this does NOT create any angle sub-jobs
    here — those are created later, by app/workers/cleanup.py, once the
    cleanup step succeeds. See that module's docstring for why (three
    independent bugs eager creation causes, spec section 4).
    """
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise IdempotencyKeyConflictError(
                "This Idempotency-Key was already used with a different request body."
            )
        return await _build_generate_with_cleanup_accepted_response(existing.id, body)

    await _enforce_rate_limit_and_quota(session, client)

    validate_operation_enabled(config_version, Operation.GENERATE_WITH_CLEANUP)

    category = find_category(config_version, body.category_code)
    if category is None:
        raise CategoryNotFoundError(
            f"Category {body.category_code} not found.",
            details={"category_code": body.category_code},
        )
    if not category["is_active"]:
        raise CategoryInactiveError(
            f"Category {category['code']} is not active.",
            details={"category_code": category["code"]},
        )

    if not body.angles:
        raise NoAnglesRequestedError("At least one angle must be requested.")
    if len(set(body.angles)) != len(body.angles):
        raise ValidationError(
            "Duplicate angles requested.", details={"angles": [a.value for a in body.angles]}
        )
    for angle in body.angles:
        angle_config = category["angles"].get(angle.value, {})
        if not angle_config.get("enabled", False):
            raise AngleNotEnabledError(
                f"Angle {angle.value} is not enabled for category {category['code']}.",
                details={"category_code": category["code"], "angle": angle.value},
            )

    if not storage_service.exists(settings.BUCKET_INPUTS, body.storage_path):
        raise AssetNotFoundError(
            f"No uploaded asset found at {body.storage_path}.",
            details={"storage_path": body.storage_path},
        )
    if not body.storage_path.startswith(f"pending/{client.id}/"):
        raise AssetNotOwnedError(
            f"storage_path {body.storage_path} does not belong to this client.",
            details={"storage_path": body.storage_path},
        )
    meta = image_validation.inspect_and_validate(settings.BUCKET_INPUTS, body.storage_path)

    job = jobs_repo.create_job(
        session,
        client_id=client.id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        category_code=body.category_code,
        config_version_id=config_version.id,
        requested_angles=len(body.angles),
        sku_reference=body.sku_reference,
        metadata=body.metadata,
        operation=Operation.GENERATE_WITH_CLEANUP,
        requested_angle_codes=[a.value for a in body.angles],
    )

    try:
        await session.flush()  # assigns job.id
    except IntegrityError:
        await session.rollback()
        return await _handle_generate_with_cleanup_replay_race(
            session, client, idempotency_key, payload_hash, body
        )

    validate_operation_angle_consistency(Operation.GENERATE_WITH_CLEANUP, None)
    asset = assets_repo.create_asset(
        session,
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket=settings.BUCKET_INPUTS,
        storage_path=body.storage_path,
        mime_type=meta.mime_type,
        width_px=meta.width_px,
        height_px=meta.height_px,
        bytes_=meta.bytes,
        checksum_sha256=meta.checksum_sha256,
        expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
    )
    await session.flush()  # assigns asset.id

    cleanup_sub_job = jobs_repo.create_sub_job(
        session,
        job_id=job.id,
        angle=None,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        input_asset_id=asset.id,
    )
    await session.flush()  # assigns cleanup_sub_job.id

    job_events_repo.record_event(
        session,
        job.id,
        "JOB_CREATED",
        to_status=job.status.value,
        detail={
            "category_code": body.category_code,
            "requested_angles": len(body.angles),
            "requested_angle_codes": [a.value for a in body.angles],
        },
    )

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return await _handle_generate_with_cleanup_replay_race(
            session, client, idempotency_key, payload_hash, body
        )

    from app.workers.cleanup import process_task

    process_task.delay(str(cleanup_sub_job.id))

    return await _build_generate_with_cleanup_accepted_response(job.id, body)


async def _handle_generate_with_cleanup_replay_race(
    session: AsyncSession,
    client: ApiClient,
    idempotency_key: str,
    payload_hash: str,
    body: "GenerateWithCleanupRequest",
) -> JobAcceptedResponse:
    existing = await jobs_repo.get_by_idempotency_key(session, client.id, idempotency_key)
    if existing is None:
        raise AppError(
            "Idempotency key conflict could not be resolved.", code=ErrorCode.INTERNAL_ERROR
        )
    if existing.payload_hash != payload_hash:
        raise IdempotencyKeyConflictError(
            "This Idempotency-Key was already used with a different request body."
        )
    return await _build_generate_with_cleanup_accepted_response(existing.id, body)


async def _build_generate_with_cleanup_accepted_response(
    job_id: uuid.UUID, body: "GenerateWithCleanupRequest"
) -> JobAcceptedResponse:
    return JobAcceptedResponse(
        job_id=str(job_id),
        status="PENDING",
        angles=[
            ResolvedAnglePlan(
                angle=angle,
                source_type=SourceType.UPLOADED,
                status=SubJobStatus.PENDING,
                storage_path=None,
            )
            for angle in body.angles
        ],
        poll_after_ms=POLL_AFTER_MS,
    )
```

Add `from app.api.v2.schemas.generate_with_cleanup import GenerateWithCleanupRequest`
to `job_service.py`'s imports (at the top, alongside the other schema
imports — check how `MatchRequest`/`RecolorRequest` are already imported
there for the exact placement and style).

- [ ] **Step 5: Run the full unit suite to confirm no import errors or regressions**

Run: `pytest tests/unit/ -v`
Expected: PASS, no new failures, no import errors

- [ ] **Step 6: Lint and type-check**

Run: `ruff check app/ tests/ && ruff format --check app/ tests/ && mypy --strict app/`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add app/api/v2/schemas/generate_with_cleanup.py app/services/job_service.py tests/unit/test_generate_with_cleanup_schema.py
git commit -m "feat: add GenerateWithCleanupRequest schema and job creation service"
```

---

### Task 8: Route + presign + `main.py` registration + integration tests

**Files:**
- Create: `app/api/v2/generate_with_cleanup.py`
- Modify: `app/main.py` (router registration)
- Modify: `app/api/v2/schemas/uploads.py:33-36` (error message string only — the validator logic already permits any non-`ANGLE_GENERATION` operation, confirmed by reading it; only the listed-operations string in the raised `ValueError` needs updating for accuracy)
- Create: `tests/integration/test_api_generate_with_cleanup.py`

**Interfaces:**
- Produces: `POST /api/v2/generate-with-cleanup` — the full client-facing surface

- [ ] **Step 1: Update the presign validator's error message**

In `app/api/v2/schemas/uploads.py`, change:

```python
        if op_mode and self.operation == Operation.ANGLE_GENERATION:
            raise ValueError(
                "operation must be BACKGROUND_REMOVAL, BACKGROUND_REPLACEMENT, MATCH, "
                "RECOLOR, or MIX (ANGLE_GENERATION uses category_code/angles instead)"
            )
```

to:

```python
        if op_mode and self.operation == Operation.ANGLE_GENERATION:
            raise ValueError(
                "operation must be BACKGROUND_REMOVAL, BACKGROUND_REPLACEMENT, MATCH, "
                "RECOLOR, MIX, or GENERATE_WITH_CLEANUP "
                "(ANGLE_GENERATION uses category_code/angles instead)"
            )
```

No other change to this file — `{"operation": "GENERATE_WITH_CLEANUP"}` already
falls through the existing `if body.operation is not None:` branch in
`app/api/v2/uploads.py` and returns a single `operation_upload` slot, since
that route's logic only special-cases `MIX`/`RECOLOR`/`BACKGROUND_REPLACEMENT`
for their *extra* slots — GENERATE_WITH_CLEANUP needs exactly one, the same
as `BACKGROUND_REMOVAL`/`MATCH` already get with zero extra code.

- [ ] **Step 2: Write the route**

```python
# app/api/v2/generate_with_cleanup.py
"""POST /api/v2/generate-with-cleanup.

See docs/api-routes.md and docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md.
"""

import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v2.schemas.generate import JobAcceptedResponse
from app.api.v2.schemas.generate_with_cleanup import GenerateWithCleanupRequest
from app.core.auth import require_client_scope
from app.core.idempotency import require_idempotency_key
from app.db.models.api_clients import ApiClient
from app.db.repositories import config_versions as config_versions_repo
from app.db.session import get_db
from app.services.config_service import ConfigUnavailableError
from app.services.job_service import create_generate_with_cleanup_job_for_request

router = APIRouter(tags=["generate-with-cleanup"])


def _payload_hash(body: BaseModel) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@router.post(
    "/generate-with-cleanup",
    status_code=202,
    response_model=JobAcceptedResponse,
    responses={
        400: {"description": "Idempotency-Key missing"},
        401: {"description": "Invalid API key"},
        409: {"description": "Idempotency key conflict"},
        422: {
            "description": (
                "Operation disabled, category not found/inactive, angle not "
                "enabled, duplicate/empty angles, or asset not found/owned/invalid"
            )
        },
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def create_generate_with_cleanup_job(
    body: GenerateWithCleanupRequest,
    client: Annotated[ApiClient, Depends(require_client_scope)],
    idempotency_key: Annotated[str, Depends(require_idempotency_key)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobAcceptedResponse:
    config_version = await config_versions_repo.get_active(session)
    if config_version is None:
        raise ConfigUnavailableError("No active config version found.")

    return await create_generate_with_cleanup_job_for_request(
        session,
        client,
        config_version,
        body,
        idempotency_key,
        _payload_hash(body),
    )
```

- [ ] **Step 3: Register the router in `main.py`**

Add `from app.api.v2 import generate_with_cleanup as generate_with_cleanup_routes`
to the imports, and `api_v2.include_router(generate_with_cleanup_routes.router)`
to the router registrations (alongside `mix_routes.router`).

- [ ] **Step 4: Write the failing integration tests**

```python
# tests/integration/test_api_generate_with_cleanup.py
"""POST /api/v2/generate-with-cleanup, operation-aware presign, and the real
two-phase dispatch/worker/status/retry machinery. See docs/superpowers/specs/
2026-08-31-generate-with-cleanup-design.md.

Same stack as tests/integration/test_api_mix.py: testcontainers Postgres,
real local Redis, real Supabase Storage (never mocked), fixture-driven
Gemini (tests/conftest.py's autouse `_fake_gemini_success_by_default`).
Under `task_always_eager` (also autouse), `POST /api/v2/generate-with-cleanup`
dispatches `cleanup.process` inline during the request -- and
`cleanup.process` itself dispatches `generation.transform_photo_task` inline
too, so a happy-path request completes the ENTIRE two-phase pipeline
synchronously within the test.
"""

import io
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.api_clients import ApiClient
from app.db.models.assets import Asset
from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import JobStatus, Operation, SubJobStatus, SyncStatus
from app.db.models.jobs import Job, SubJob
from app.db.session import get_db
from app.main import app
from app.providers.gemini import GeminiProvider
from app.services import storage_service
from scripts.seed_dev import CATEGORY_PAYLOAD

pytestmark = pytest.mark.integration

_hasher = PasswordHasher()
_GEMINI_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gemini"


def _load_gemini_fixture(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((_GEMINI_FIXTURES / name).read_text())
    return result


def _cleanup_payload() -> dict[str, Any]:
    """CATEGORY_PAYLOAD doesn't seed operations.GENERATE_WITH_CLEANUP by
    default -- only migration 0022 does, against the real DB. Build a
    payload with it enabled on top of it, same technique test_api_mix.py's
    own _mix_payload uses."""
    payload = dict(CATEGORY_PAYLOAD)
    payload["global"] = dict(payload["global"])
    payload["global"]["operations"] = {
        **payload["global"]["operations"],
        "GENERATE_WITH_CLEANUP": {
            "enabled": True,
            "prompt": "Remove the background, standardize on a clean e-commerce backdrop.",
            "unit_cost_usd": 0.02,
        },
    }
    return payload


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _make_client(
    db_session: AsyncSession, name: str, scope: str = "client"
) -> tuple[ApiClient, str]:
    import secrets

    raw = secrets.token_urlsafe(32)
    api_client = ApiClient(
        name=name, key_prefix=raw[:8], key_hash=_hasher.hash(raw), scope=scope, is_active=True
    )
    db_session.add(api_client)
    await db_session.flush()
    return api_client, raw


@pytest.fixture
async def active_config(db_session: AsyncSession) -> ConfigVersion:
    cv = ConfigVersion(
        version_number=1,
        source_hash="generate-with-cleanup-test-hash",
        payload=_cleanup_payload(),
        sync_status=SyncStatus.SUCCESS,
        is_active=True,
        activated_at=datetime.now(UTC),
    )
    db_session.add(cv)
    await db_session.commit()
    return cv


@pytest.fixture
async def api_client_key(db_session: AsyncSession, active_config: ConfigVersion) -> str:
    _, raw = await _make_client(db_session, "generate-with-cleanup-test-client")
    await db_session.commit()
    return raw


def _real_jpeg_bytes(size: tuple[int, int] = (60, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 10, 200)).save(buf, format="JPEG")
    return buf.getvalue()


async def _presign_and_upload(client: AsyncClient, key: str) -> str:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": key},
        json={"operation": "GENERATE_WITH_CLEANUP"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation_upload"] is not None
    upload = body["operation_upload"]

    put = httpx.put(
        upload["upload_url"], content=_real_jpeg_bytes(), headers={"Content-Type": "image/jpeg"}
    )
    assert put.status_code == 200
    return str(upload["storage_path"])


async def test_presign_operation_mode_accepts_generate_with_cleanup(
    client: AsyncClient, api_client_key: str
) -> None:
    resp = await client.post(
        "/api/v2/uploads/presign",
        headers={"X-API-Key": api_client_key},
        json={"operation": "GENERATE_WITH_CLEANUP"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["angles"] == []
    assert body["operation_upload"] is not None
    assert body["mask_upload"] is None
    assert body["secondary_upload"] is None


async def test_happy_path_creates_cleanup_sub_job_then_angle_sub_jobs_from_its_output(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    """The test that proves the pipeline actually chains: every angle
    sub-job's input_asset_id must be the CLEANUP sub-job's output asset --
    NOT the client's original upload.
    """
    storage_path = await _presign_and_upload(client, api_client_key)

    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-happy-path"},
        json={
            "storage_path": storage_path,
            "category_code": "RING",
            "angles": ["FRONT", "SIDE", "DIAGONAL"],
        },
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    ).scalar_one()
    assert job.status == JobStatus.COMPLETED
    assert job.operation == Operation.GENERATE_WITH_CLEANUP

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 4  # 1 cleanup + 3 angles

    cleanup_sub_job = next(sj for sj in sub_jobs if sj.angle is None)
    assert cleanup_sub_job.status == SubJobStatus.COMPLETED
    assert cleanup_sub_job.output_asset_id is not None

    angle_sub_jobs = [sj for sj in sub_jobs if sj.angle is not None]
    assert len(angle_sub_jobs) == 3
    assert {sj.angle.value for sj in angle_sub_jobs} == {"FRONT", "SIDE", "DIAGONAL"}
    for angle_sub_job in angle_sub_jobs:
        assert angle_sub_job.status == SubJobStatus.COMPLETED
        assert angle_sub_job.input_asset_id == cleanup_sub_job.output_asset_id


async def test_cleanup_failure_fails_the_job_with_zero_angle_sub_jobs(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import ProviderError
    from app.db.models.enums import FailureClass

    def _refuse(self: object, prompt: str, reference_images: list[bytes], seed: int) -> None:
        raise ProviderError("refused.", failure_class=FailureClass.SAFETY_REFUSAL)

    monkeypatch.setattr(GeminiProvider, "generate", _refuse)

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-fails"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    ).scalar_one()
    assert job.status == JobStatus.FAILED

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 1  # the cleanup sub-job only -- no angle sub-jobs ever created
    assert sub_jobs[0].angle is None
    assert sub_jobs[0].status == SubJobStatus.REJECTED  # SAFETY_REFUSAL -> REJECTED, not FAILED


async def test_status_never_exposes_the_cleanup_sub_job(
    client: AsyncClient, api_client_key: str
) -> None:
    """The user's explicit choice: internal only, never in results, never
    in angles."""
    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-not-exposed"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]

    status_resp = await client.get(
        f"/api/v2/status/{job_id}", headers={"X-API-Key": api_client_key}
    )
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["operation"] == "GENERATE_WITH_CLEANUP"
    assert body["results"] == []
    assert len(body["angles"]) == 1
    assert body["angles"][0]["angle"] == "FRONT"
    assert body["angles"][0]["status"] == "COMPLETED"


async def test_empty_angles_list_returns_no_angles_requested(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-no-angles"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": []},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "NO_ANGLES_REQUESTED"


async def test_duplicate_angles_rejected(client: AsyncClient, api_client_key: str) -> None:
    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-dup-angles"},
        json={
            "storage_path": storage_path,
            "category_code": "RING",
            "angles": ["FRONT", "FRONT"],
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_idempotent_replay_returns_original_job_id(
    client: AsyncClient, api_client_key: str
) -> None:
    storage_path = await _presign_and_upload(client, api_client_key)
    payload = {"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]}
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-replay"}

    first = await client.post("/api/v2/generate-with-cleanup", headers=headers, json=payload)
    second = await client.post("/api/v2/generate-with-cleanup", headers=headers, json=payload)
    assert first.json()["job_id"] == second.json()["job_id"]


async def test_same_key_different_payload_409(client: AsyncClient, api_client_key: str) -> None:
    storage_path = await _presign_and_upload(client, api_client_key)
    headers = {"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-conflict"}
    await client.post(
        "/api/v2/generate-with-cleanup",
        headers=headers,
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers=headers,
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["SIDE"]},
    )
    assert resp.status_code == 409, resp.text


async def test_job_level_retry_retries_a_failed_cleanup_step(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the cleanup sub-job exists when cleanup fails -- job-level retry
    (§14's all-or-nothing logic, a set of size one) retries exactly it, and
    on success creates and dispatches the angle sub-jobs through the
    identical path the original request used."""
    from app.core.errors import ProviderError
    from app.db.models.enums import FailureClass

    call_count = {"n": 0}
    original_generate = GeminiProvider.generate

    def _fail_once_then_succeed(
        self: GeminiProvider, prompt: str, reference_images: list[bytes], seed: int
    ):  # noqa: ANN201
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ProviderError("refused.", failure_class=FailureClass.SAFETY_REFUSAL)
        return original_generate(self, prompt, reference_images, seed)

    monkeypatch.setattr(GeminiProvider, "generate", _fail_once_then_succeed)

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-retry"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]
    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    ).scalar_one()
    assert job.status == JobStatus.FAILED

    # SAFETY_REFUSAL -> REJECTED, not FAILED, and REJECTED is not retryable
    # (docs/business-rules.md §4). Use a retryable failure class instead so
    # this test actually exercises the retry path -- swap the monkeypatch
    # for one that raises TRANSIENT_PROVIDER on the first call.
```

**Stop and fix Step 4's last test before writing it as shown** — `SAFETY_REFUSAL`
produces `REJECTED`, which `docs/business-rules.md` §4 explicitly marks
non-retryable (`retryable: false`, no retry button offered). Rewrite
`test_job_level_retry_retries_a_failed_cleanup_step` to raise
`FailureClass.TRANSIENT_PROVIDER` on the first call instead of
`SAFETY_REFUSAL`, so the cleanup sub-job lands `FAILED` (not `REJECTED`) and
is genuinely retryable:

```python
async def test_job_level_retry_retries_a_failed_cleanup_step(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors import ProviderError
    from app.db.models.enums import FailureClass

    call_count = {"n": 0}
    original_generate = GeminiProvider.generate

    def _fail_once_then_succeed(
        self: GeminiProvider, prompt: str, reference_images: list[bytes], seed: int
    ):  # noqa: ANN201
        call_count["n"] += 1
        if call_count["n"] <= 3:  # exhaust all 3 in-process attempts
            raise ProviderError("blip.", failure_class=FailureClass.TRANSIENT_PROVIDER)
        return original_generate(self, prompt, reference_images, seed)

    monkeypatch.setattr(GeminiProvider, "generate", _fail_once_then_succeed)

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-retry"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]
    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    ).scalar_one()
    assert job.status == JobStatus.FAILED

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 1
    assert sub_jobs[0].status == SubJobStatus.FAILED  # TRANSIENT_PROVIDER -> FAILED, retryable

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-retry-2"},
    )
    assert retry_resp.status_code == 202, retry_resp.text

    await db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED

    sub_jobs = (
        (await db_session.execute(select(SubJob).where(SubJob.job_id == job.id))).scalars().all()
    )
    assert len(sub_jobs) == 2  # cleanup (now COMPLETED) + the 1 angle it dispatched
    angle_sub_job = next(sj for sj in sub_jobs if sj.angle is not None)
    assert angle_sub_job.status == SubJobStatus.COMPLETED


async def test_job_level_retry_blocked_once_angle_sub_jobs_exist(
    client: AsyncClient, api_client_key: str
) -> None:
    """Once cleanup has succeeded and angle sub-jobs exist, job-level retry
    must reject with a clear redirect to the per-angle route -- exactly the
    same posture ANGLE_GENERATION jobs already have, conditional here on
    whether the pipeline has moved past its cleanup phase."""
    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-past-phase-1"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-blocked-retry"},
    )
    assert retry_resp.status_code == 409, retry_resp.text


async def test_per_angle_retry_works_once_cleanup_has_succeeded(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing per-angle route works completely unmodified once the
    angle sub-job's input_asset_id already points at the cleanup output."""
    from app.core.errors import ProviderError
    from app.db.models.enums import FailureClass

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-then-angle-fails"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT"]},
    )
    job_id = resp.json()["job_id"]
    job = (
        await db_session.execute(select(Job).where(Job.id == uuid.UUID(job_id)))
    ).scalar_one()
    assert job.status == JobStatus.COMPLETED  # cleanup + angle both succeeded via fixture

    # Force the already-completed angle sub-job back to FAILED to exercise
    # the retry route in isolation, rather than re-running the whole
    # pipeline with a more elaborate call-counting monkeypatch.
    angle_sub_job = (
        (
            await db_session.execute(
                select(SubJob).where(SubJob.job_id == job.id, SubJob.angle.is_not(None))
            )
        )
        .scalars()
        .one()
    )
    angle_sub_job.status = SubJobStatus.FAILED
    angle_sub_job.failure_class = FailureClass.TRANSIENT_PROVIDER
    angle_sub_job.attempt_count = 1
    await db_session.commit()

    retry_resp = await client.post(
        f"/api/v2/jobs/{job_id}/angles/FRONT/retry",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "angle-retry"},
    )
    assert retry_resp.status_code == 202, retry_resp.text

    await db_session.refresh(angle_sub_job)
    assert angle_sub_job.status == SubJobStatus.COMPLETED


async def test_cost_report_includes_cleanup_and_angle_calls(
    client: AsyncClient, api_client_key: str, db_session: AsyncSession
) -> None:
    from app.db.models.cost_events import CostEvent

    storage_path = await _presign_and_upload(client, api_client_key)
    resp = await client.post(
        "/api/v2/generate-with-cleanup",
        headers={"X-API-Key": api_client_key, "Idempotency-Key": "cleanup-cost"},
        json={"storage_path": storage_path, "category_code": "RING", "angles": ["FRONT", "SIDE"]},
    )
    job_id = resp.json()["job_id"]

    events = (
        (
            await db_session.execute(
                select(CostEvent).where(CostEvent.job_id == uuid.UUID(job_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 3  # 1 cleanup call + 2 angle calls
```

- [ ] **Step 5: Run test to verify each fails for the right reason**

Run: `pytest tests/integration/test_api_generate_with_cleanup.py -v`
Expected: FAIL — `404` on `/api/v2/generate-with-cleanup` (route not yet
registered) before Step 2/3 are applied; after applying them, re-run and
expect all tests to attempt real execution

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/integration/test_api_generate_with_cleanup.py -v`
Expected: PASS, all tests. If `test_happy_path_creates_cleanup_sub_job_then_angle_sub_jobs_from_its_output`
fails with a mismatch on `input_asset_id`, the bug is in Task 6's
`_dispatch_angle_phase` — verify it is really passing
`cleanup_sub_job.output_asset_id`, not `cleanup_sub_job.input_asset_id`, to
`jobs_repo.create_sub_job`.

- [ ] **Step 7: Run the full test suite to confirm no regressions**

Run: `pytest -q`
Expected: every previously-passing test still passes; only new tests added

- [ ] **Step 8: Lint and type-check everything touched**

Run: `ruff check app/ tests/ migrations/ scripts/ && ruff format --check app/ tests/ migrations/ scripts/ && mypy --strict app/`
Expected: clean

- [ ] **Step 9: Commit**

```bash
git add app/api/v2/generate_with_cleanup.py app/api/v2/schemas/uploads.py app/main.py tests/integration/test_api_generate_with_cleanup.py
git commit -m "feat: add POST /generate-with-cleanup route and integration tests"
```

---

### Task 9: `retry.py` — job-level retry guard and dispatch entry

**Files:**
- Modify: `app/api/v2/retry.py`

**Interfaces:**
- Consumes: `cleanup.process_task` (Task 6)
- Produces: `retry_job` now handles `GENERATE_WITH_CLEANUP` correctly (guard + dispatch)

Task 8's integration tests (`test_job_level_retry_retries_a_failed_cleanup_step`,
`test_job_level_retry_blocked_once_angle_sub_jobs_exist`) already exercise
this behavior end-to-end and currently fail without this task's change —
this task is what makes them pass. If Task 8 was executed strictly in order,
these two tests are the "write the failing test" step for this task; do not
write new tests here, just make the existing ones from Task 8 pass.

- [ ] **Step 1: Confirm the two retry tests currently fail for the right reason**

Run: `pytest tests/integration/test_api_generate_with_cleanup.py::test_job_level_retry_retries_a_failed_cleanup_step tests/integration/test_api_generate_with_cleanup.py::test_job_level_retry_blocked_once_angle_sub_jobs_exist -v`

Expected: `test_job_level_retry_retries_a_failed_cleanup_step` fails at the
retry dispatch step — `cleanup_process_task` isn't in `retry_job`'s dispatch
lookup, so it falls through to `background_process_task` and crashes on
`assert sub_job.angle is None` inside `background_service.process` (it IS
None for the cleanup sub-job, so this particular assert won't be what fails
— look instead for a crash inside `background_service.process` from
resolving `GENERATE_WITH_CLEANUP`'s prompt via `find_operation_config`,
which will find real config but the wrong cost label / assumptions from a
different operation's shape; if it happens not to crash, it will still
silently mishandle the job). `test_job_level_retry_blocked_once_angle_sub_jobs_exist`
fails because no guard exists yet — it currently returns `202`, not `409`.

- [ ] **Step 2: Add the guard and dispatch entry**

In `app/api/v2/retry.py`, immediately after the existing
`sub_jobs = await jobs_repo.get_sub_jobs(session, job.id)` /
`if not sub_jobs: raise NotFoundError(...)` block inside `retry_job`, add:

```python
    # 2026-08-31: GENERATE_WITH_CLEANUP defers creating its angle sub-jobs
    # until its cleanup step succeeds (docs/superpowers/specs/
    # 2026-08-31-generate-with-cleanup-design.md section 4) — so job-level
    # retry is valid ONLY while the job hasn't moved past that step yet.
    # Once any angle sub-job exists, retrying a specific angle must go
    # through the per-angle route, same posture ANGLE_GENERATION jobs
    # always have, just conditional here on pipeline phase rather than
    # unconditional on operation.
    if job.operation == Operation.GENERATE_WITH_CLEANUP and any(
        sj.angle is not None for sj in sub_jobs
    ):
        raise AngleJobRetryNotAllowedError(
            "This job has moved past its cleanup step — retry a specific "
            "angle via POST /jobs/{job_id}/angles/{angle}/retry.",
            details={"job_id": job_id},
        )
```

Then update the dispatch lookup near the bottom of the function:

```python
    from app.workers.background import process_task as background_process_task
    from app.workers.cleanup import process_task as cleanup_process_task
    from app.workers.match import process_task as match_process_task
    from app.workers.mix import process_task as mix_process_task
    from app.workers.recolor import process_task as recolor_process_task

    dispatch_task = {
        Operation.MATCH: match_process_task,
        Operation.RECOLOR: recolor_process_task,
        Operation.MIX: mix_process_task,
        Operation.GENERATE_WITH_CLEANUP: cleanup_process_task,
    }.get(job.operation, background_process_task)
    for sub_job in failed:
        dispatch_task.delay(str(sub_job.id))
```

- [ ] **Step 3: Run the two tests to verify they pass**

Run: `pytest tests/integration/test_api_generate_with_cleanup.py::test_job_level_retry_retries_a_failed_cleanup_step tests/integration/test_api_generate_with_cleanup.py::test_job_level_retry_blocked_once_angle_sub_jobs_exist -v`
Expected: PASS, both

- [ ] **Step 4: Run the full retry test suite to confirm no regression**

Run: `pytest tests/integration/ -k retry -v`
Expected: PASS — MATCH's/RECOLOR's/MIX's/background's own job-level retry
behavior must be byte-identical to before this change (the guard only
triggers for `GENERATE_WITH_CLEANUP`, and the dispatch lookup's `.get(...,
background_process_task)` fallback is unchanged for every other operation)

- [ ] **Step 5: Run the full test suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 6: Lint and type-check**

Run: `ruff check app/api/v2/retry.py && ruff format --check app/api/v2/retry.py && mypy --strict app/api/v2/retry.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add app/api/v2/retry.py
git commit -m "feat: job-level retry supports GENERATE_WITH_CLEANUP's cleanup step"
```

---

### Task 10: `GET /status` — the phase-1 synthesized angle list

**Files:**
- Modify: `app/services/status_service.py` (new helper)
- Modify: `app/api/v2/status.py` (new branch)
- Test: extend `tests/integration/test_api_generate_with_cleanup.py`

**Interfaces:**
- Produces: `build_pending_angle_status(angle: Angle) -> AngleStatus` (new, `status_service.py`)

During phase 1 (cleanup still running, no angle sub-jobs exist yet),
`GET /status` must not return `angles: []` — that would look identical to
"nothing was requested," when the job is genuinely `PROCESSING`. Synthesize
a `PENDING` entry per angle in `job.requested_angle_codes` instead, so the
response shape matches what `/generate` already shows immediately at
request time.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_api_generate_with_cleanup.py`:

```python
async def test_status_shows_synthesized_pending_angles_during_cleanup_phase(
    client: AsyncClient,
    api_client_key: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before any angle sub-job exists, GET /status must show a PENDING
    entry per requested angle -- not an empty array, which would be
    indistinguishable from "nothing requested"."""
    from app.providers.gemini import GeminiProvider

    def _hang(self: GeminiProvider, prompt: str, reference_images: list[bytes], seed: int):  # noqa: ANN201
        # Simulates cleanup still running by never letting the cleanup
        # Gemini call return -- but task_always_eager means the whole
        # request runs synchronously, so we can't actually observe an
        # in-flight PROCESSING state via a real HTTP round trip here.
        # Instead, directly construct the DB state phase 1 leaves behind
        # and assert the status-building function handles it, exercised
        # through a real GET /status call against a job in that exact shape.
        raise NotImplementedError

    storage_path = await _presign_and_upload(client, api_client_key)

    # Build a job in exactly the state phase 1 leaves it in, without going
    # through the full async pipeline (which completes synchronously under
    # task_always_eager and leaves no observable in-between state over
    # real HTTP) -- same technique used elsewhere in this test suite for
    # states that are real but transient in production.
    from app.db.repositories import assets as assets_repo
    from app.db.repositories import jobs as jobs_repo
    from app.db.models.enums import AssetKind, JobStatus, SourceType, SubJobStatus
    from app.services import job_service, retention_policy

    resp = await client.get("/api/v2/config", headers={"X-API-Key": api_client_key})
    config_version_id = None  # not needed directly; use active_config fixture value instead

    # Simpler: create the job/sub-job directly via the repository layer,
    # mirroring what create_generate_with_cleanup_job_for_request does up
    # to (but not including) dispatching cleanup.process.
    from app.db.repositories import config_versions as config_versions_repo

    cv = await config_versions_repo.get_active(db_session)
    assert cv is not None

    job = jobs_repo.create_job(
        db_session,
        client_id=(await db_session.execute(
            select(ApiClient).where(ApiClient.key_prefix == api_client_key[:8])
        )).scalar_one().id,
        idempotency_key="phase-1-status-test",
        payload_hash="phase-1-status-test-hash",
        category_code="RING",
        config_version_id=cv.id,
        requested_angles=2,
        sku_reference=None,
        metadata={},
        operation=Operation.GENERATE_WITH_CLEANUP,
        requested_angle_codes=["FRONT", "SIDE"],
    )
    await db_session.flush()
    job.status = JobStatus.PROCESSING
    asset = assets_repo.create_asset(
        db_session,
        job_id=job.id,
        kind=AssetKind.INPUT,
        bucket="jewelry-inputs",
        storage_path=storage_path,
        mime_type="image/jpeg",
        expires_at=retention_policy.compute_expires_at(AssetKind.INPUT),
    )
    await db_session.flush()
    jobs_repo.create_sub_job(
        db_session,
        job_id=job.id,
        angle=None,
        status=SubJobStatus.GENERATING,
        source_type=SourceType.UPLOADED,
        input_asset_id=asset.id,
    )
    await db_session.commit()

    status_resp = await client.get(
        f"/api/v2/status/{job.id}", headers={"X-API-Key": api_client_key}
    )
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "PROCESSING"
    assert len(body["angles"]) == 2
    assert {a["angle"] for a in body["angles"]} == {"FRONT", "SIDE"}
    for angle_status in body["angles"]:
        assert angle_status["status"] == "PENDING"
        assert angle_status["image_url"] is None
        assert angle_status["retryable"] is False
```

Remove the unused `_hang`/`config_version_id` scaffolding left over from
drafting — keep only the direct-construction path shown from `from app.db.repositories import assets as assets_repo` onward. Add
`from app.db.models.api_clients import ApiClient` to the file's imports if
not already present (it is, from the top-level import list).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_api_generate_with_cleanup.py::test_status_shows_synthesized_pending_angles_during_cleanup_phase -v`
Expected: FAIL — `assert len(body["angles"]) == 2` fails because `angles`
is currently `[]` (the route falls through to the `ANGLE_GENERATION`-only
branch check, finds `operation != ANGLE_GENERATION`, and — before this
task — has no matching branch at all, so it falls all the way to the
background-result branch and crashes on `assert sub_job.angle is None` for
the wrong reason, or returns an empty `results`/`angles`; either way the
assertion on angle count fails)

- [ ] **Step 3: Add `build_pending_angle_status` to `status_service.py`**

```python
def build_pending_angle_status(angle: Angle) -> AngleStatus:
    """Synthesizes a PENDING entry for an angle whose sub-job does not
    exist yet — GENERATE_WITH_CLEANUP defers creating angle sub-jobs until
    its cleanup step succeeds (docs/superpowers/specs/
    2026-08-31-generate-with-cleanup-design.md section 5), so during that
    phase GET /status would otherwise show an empty `angles` array,
    indistinguishable from "nothing was requested." This makes the response
    shape match what /generate already shows immediately at request time.
    """
    return AngleStatus(
        angle=angle,
        status=SubJobStatus.PENDING,
        source_type=SourceType.UPLOADED,
        synthetic=False,
        image_url=None,
        qa_status=QAStatus.NOT_APPLICABLE,
        qa_score=None,
        failure_class=None,
        error_message=None,
        retryable=False,
        retry_url=None,
    )
```

Add `from app.db.models.enums import Angle` and `QAStatus` to
`status_service.py`'s imports if not already present (check the existing
import line — `QAStatus` is already imported there for
`build_background_result_status`; only `Angle` may be new).

- [ ] **Step 4: Add the branch to `status.py`**

In `app/api/v2/status.py`, insert a new branch before the existing
`if job.operation == Operation.ANGLE_GENERATION:` check:

```python
    if job.operation == Operation.GENERATE_WITH_CLEANUP:
        angle_sub_jobs = [sj for sj in sub_jobs if sj.angle is not None]
        if not angle_sub_jobs:
            # Phase 1: cleanup still running (or failed -- but a failed
            # cleanup makes the PARENT job FAILED via the unmodified
            # rollup, which this same response already reflects via
            # `status`; the synthesized angles here just mean "no angle
            # sub-job exists yet," which is also true on a cleanup
            # failure, and is not misleading paired with status: FAILED).
            assert job.requested_angle_codes is not None
            angle_statuses = [
                status_service.build_pending_angle_status(Angle(code))
                for code in job.requested_angle_codes
            ]
        else:
            angle_statuses = [
                status_service.build_angle_status(job, sub_job, bucket_and_paths[sub_job.id])
                for sub_job in angle_sub_jobs
            ]
        return status_service.build_job_status_response(job, angle_statuses)
```

Add `Angle` to `status.py`'s import from `app.db.models.enums` (it currently
imports only `Operation` from there — extend to
`from app.db.models.enums import Angle, Operation`).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_api_generate_with_cleanup.py::test_status_shows_synthesized_pending_angles_during_cleanup_phase -v`
Expected: PASS

- [ ] **Step 6: Run the full test suite**

Run: `pytest -q`
Expected: all pass, including every prior `GET /status` test for every
other operation — this branch is additive, gated strictly on
`job.operation == Operation.GENERATE_WITH_CLEANUP`

- [ ] **Step 7: Lint and type-check**

Run: `ruff check app/services/status_service.py app/api/v2/status.py tests/integration/test_api_generate_with_cleanup.py && ruff format --check app/services/status_service.py app/api/v2/status.py tests/integration/test_api_generate_with_cleanup.py && mypy --strict app/services/status_service.py app/api/v2/status.py`
Expected: clean

- [ ] **Step 8: Commit**

```bash
git add app/services/status_service.py app/api/v2/status.py tests/integration/test_api_generate_with_cleanup.py
git commit -m "feat: synthesize pending angle entries during GENERATE_WITH_CLEANUP's cleanup phase"
```

---

### Task 11: Documentation

**Files:**
- Modify: `docs/api-routes.md` (new "Cleanup + Angle Generation" section, mirroring the existing per-operation sections)
- Modify: `docs/business-rules.md` (new §17, mirroring §13-16's structure)
- Modify: `docs/ai-integration.md` (new "Mode G" under Call Site 1, mirroring Modes C-F)
- Modify: `docs/schema.md` (note `operation_t`'s new value and `jobs.requested_angle_codes`, mirroring how migrations 0006/0013/0015/0017 are each documented there)
- Modify: `CLAUDE.md` (dated correction note, same style as the 2026-08-31 MIX rewrite note already there)

This project's own convention (`docs/conventions.md`: "A phase is not
complete until docs/ reflects what was actually built. Documentation drift
is a bug") requires this before the feature is genuinely done — not optional
polish.

- [ ] **Step 1: Update `docs/schema.md`**

In the `operation_t` enum block's prose (near where `MIX` was added by
migration 0017), add a paragraph:

```markdown
`GENERATE_WITH_CLEANUP` was added by migration 0021 (2026-08-31), same
`ALTER TYPE ... ADD VALUE` mechanism. Unlike every prior operation, its
sub-jobs are heterogeneous within one job — one angle-less "cleanup"
sub-job plus 1-4 angled sub-jobs, created in two phases rather than all at
request time. See `docs/business-rules.md` §17 for the full contract.
```

In the `jobs` table's column list, add a row after `preset_code`:

```markdown
| `requested_angle_codes` | JSONB NULL | Set only for `GENERATE_WITH_CLEANUP` — the requested angle codes, durably recorded so the worker can create angle sub-jobs after the cleanup step succeeds, once the original request body is gone (migration 0021). NULL for every other operation. |
```

- [ ] **Step 2: Update `docs/api-routes.md`**

Add a new section after "Two-Piece Combination (Phase 20; generative since
2026-08-31)" and before "## Ops":

```markdown
---

## Cleanup + Angle Generation

`GENERATE_WITH_CLEANUP` — one uploaded photo in, a background-cleaned
version of it consumed internally, then 1-4 catalogue angles generated
*from that cleaned image* out — `POST /api/v2/generate-with-cleanup`. See
`docs/business-rules.md` §17. Reuses the existing job/sub-job state
machine, `GET /status/{job_id}`, cost recording, and audit trail — no
parallel pipeline, but unlike every prior operation this one's sub-jobs
are created in two phases rather than all at request time.

### `POST /api/v2/generate-with-cleanup`
**Auth required. `client` scope. `Idempotency-Key` header required.**

Request: `{ "storage_path": "...", "category_code": "...", "angles": ["FRONT", "SIDE", ...], "sku_reference"?: "...", "metadata"?: {} }`.
`storage_path` comes from `POST /uploads/presign`'s `{"operation": "GENERATE_WITH_CLEANUP"}`
response. Unlike `/generate`'s per-angle object (`storage_path` / `synthetic`
/ `skip` per angle), `angles` here is a plain list of angle codes — every
angle derives from the one uploaded photo's cleaned output, so there is
nothing per-angle to choose. Mixing in synthetic angles is not supported.

Returns `202` with the same `JobAcceptedResponse` shape as `/generate` —
`job_id`, `status: PENDING`, `poll_after_ms`, and an `angles` array with one
`{angle, status: PENDING, source_type: UPLOADED}` entry per requested angle
(`storage_path` is always `null` on each entry — there is no per-angle
upload).

**Validation, in order — all failures are `4xx` before any job row is
created:**

1. `operations.GENERATE_WITH_CLEANUP.enabled` is `true` in the active
   config version (`422 OPERATION_DISABLED` otherwise)
2. `category_code` exists and is active — `422 CATEGORY_NOT_FOUND` / `CATEGORY_INACTIVE`
3. At least one angle requested, no duplicates — `422 NO_ANGLES_REQUESTED` / `422 VALIDATION_ERROR`
4. Every requested angle is `enabled` for that category — `422 ANGLE_NOT_ENABLED`
5. `storage_path` exists in `jewelry-inputs` and belongs to this client, and
   the uploaded image passes `image_validation.inspect_and_validate` —
   `422 ASSET_NOT_FOUND` / `ASSET_NOT_OWNED` / `VALIDATION_ERROR`

**Idempotency:** same `(client_id, Idempotency-Key)` durable dedup as
`/generate`.

On acceptance, creates one job (`operation: GENERATE_WITH_CLEANUP`,
`requested_angles: N`, `requested_angle_codes: [...]`) and **one** sub-job
— the cleanup step (`angle: null`) — then dispatches `cleanup.process`
directly. **No angle sub-jobs exist yet.** Once the cleanup step reaches
`COMPLETED`, the worker layer creates the N angle sub-jobs (each
`input_asset_id` pointing at the cleanup step's own output, not the
client's original upload) and dispatches the standard angle-generation path
for each, unmodified.

### Status and retry
`GET /api/v2/status/{job_id}` — read `operation: "GENERATE_WITH_CLEANUP"`
first, then `angles` (never `results` or `variants`). **The cleanup step
itself never appears anywhere in this response** — it is purely internal.
Before it completes, `angles` shows a synthesized `PENDING` entry per
requested angle rather than an empty array, so polling behaves identically
to `/generate` from the client's point of view.

`POST /api/v2/jobs/{job_id}/retry` retries the cleanup step **only while no
angle sub-job yet exists** — once any does, the job has moved past its
cleanup phase and retry must name a specific angle via
`POST /jobs/{job_id}/angles/{angle}/retry` instead
(`409 ANGLE_JOB_RETRY_NOT_ALLOWED`), the same posture `ANGLE_GENERATION`
jobs always have, applied here conditionally on pipeline phase rather than
unconditionally on operation.
```

- [ ] **Step 3: Update `docs/business-rules.md`**

Add a new `## 17. GENERATE_WITH_CLEANUP (2026-08-31)` section after §16
(MIX), following the exact structural pattern §13-16 already use
(operation matrix table, bullet list of rule deviations, a prose section
on the mechanism, a "Retry" closing paragraph). Base its content directly
on `docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md`
sections 3-7 — that design doc is the source of truth for every claim this
section should make; do not invent behavior not already implemented in
Tasks 1-10.

Also add one sentence to §7 (QA gate)'s closing summary, alongside the
existing tally of which operations get a gate and which don't:
"`GENERATE_WITH_CLEANUP`'s cleanup step has no QA gate either — same
posture Mode A real-photo angles already have, for the same reason: it is
never the client-facing deliverable. The angle sub-jobs it later creates
are ordinary Mode A angle sub-jobs and follow that operation's existing
no-QA-gate rule unmodified."

- [ ] **Step 4: Update `docs/ai-integration.md`**

Add a `### Mode G — Cleanup-then-angles (GENERATE_WITH_CLEANUP, 2026-08-31)`
subsection under "Call site 1 — Image generation", after Mode F, following
the same table-plus-prose structure Modes C-F use. State plainly: the
cleanup call is Mode A's own background-removal transformation, reused
verbatim as an internal pipeline stage (migration 0022 seeds it with
`BACKGROUND_REMOVAL`'s exact prompt text); the angle-generation calls that
follow are ordinary Mode A calls, completely unmodified, differing only in
that their input photo is the cleanup step's output rather than a client
upload.

- [ ] **Step 5: Add the `CLAUDE.md` correction note**

Add a new dated correction after the most recent one (the 2026-08-31 MIX
rewrite note), following that note's own structure and tone — what changed,
why, what tradeoffs were accepted, what's still open. Keep it under ~200
words; point to the spec and plan files for full detail rather than
repeating them.

- [ ] **Step 6: Run the OpenAPI spec export and check for drift**

Run: `python scripts/export_openapi.py`
Expected: `docs/openapi.json` updates to include the new route; if CI
diff-checks this file (`docs/openapi.json` is committed per `docs/schema.md`'s
folder-structure note), commit the updated file too.

- [ ] **Step 7: Commit**

```bash
git add docs/schema.md docs/api-routes.md docs/business-rules.md docs/ai-integration.md CLAUDE.md docs/openapi.json
git commit -m "docs: document GENERATE_WITH_CLEANUP across schema/api-routes/business-rules/ai-integration"
```

---

### Task 12: Full suite verification and PR

**Files:** none (verification only)

- [ ] **Step 1: Run the complete test suite exactly as CI does**

Run: `ruff check app/ tests/ migrations/ scripts/ && ruff format --check app/ tests/ migrations/ scripts/ && mypy --strict app/ && pytest --cov=app --cov-report=term-missing`
Expected: all clean, all pass

- [ ] **Step 2: Verify the migration chain applies cleanly from scratch**

Run: `alembic downgrade base && alembic upgrade head`
Expected: no errors in either direction — this is the check that catches a
`downgrade()` that silently doesn't reverse its own `upgrade()`

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/generate-with-cleanup
gh pr create --title "feat: GENERATE_WITH_CLEANUP — background cleanup then multi-angle generation, one call" --body "$(cat <<'EOF'
## Summary
- New operation: one uploaded photo is background-cleaned, then 1-4
  catalogue angles are generated from that cleaned image — one API call,
  one job_id, two internal phases.
- Angle sub-jobs are created only after the cleanup step succeeds, not
  eagerly — this avoids three independent bugs a first draft hit (the
  reconciliation sweep failing pre-created PENDING rows, an illegal
  REJECTED->PENDING sub-job transition, and an all-or-nothing retry
  deadlock). See the design spec for the full accounting.
- The cleaned photo is never exposed to the client — internal only.
- The cleanup step skips background removal's mandatory QA gate
  deliberately, matching Mode A angle generation's own no-gate posture —
  otherwise a client's single call could block on human review.

## Design
docs/superpowers/specs/2026-08-31-generate-with-cleanup-design.md

## Test plan
- [ ] Full suite green (`pytest -q`)
- [ ] ruff + mypy --strict clean
- [ ] Migration chain verified both directions
- [ ] Happy path proven to chain: angle sub-jobs' input_asset_id is the
      cleanup output, not the client's original upload
- [ ] Cleanup failure creates zero angle sub-jobs, fails the job outright
- [ ] Cleanup sub-job never appears in any client-facing response
- [ ] Job-level retry works for a failed cleanup step, and is blocked once
      angle sub-jobs exist
- [ ] Per-angle retry works unmodified once cleanup has succeeded

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Report the PR URL back to the user**

---

## Self-Review Notes (fixed inline during plan-writing, listed here for the record)

- **Task 6, `_get_cleanup_sub_job`:** the first draft of this helper queried
  the wrong table (`Job` instead of `SubJob`). Caught and corrected in place
  — Task 6 Step 3 now shows only the corrected version at the point an
  executor would actually copy it, with the broken stub called out
  explicitly as "must not be copied."
- **Task 8, retry test using `SAFETY_REFUSAL`:** the first draft of
  `test_job_level_retry_retries_a_failed_cleanup_step` used
  `SAFETY_REFUSAL`, which produces `REJECTED` — a status
  `docs/business-rules.md` §4 explicitly marks non-retryable. Caught and
  corrected to `TRANSIENT_PROVIDER` (which produces `FAILED`, genuinely
  retryable) before the task's final listing.
- **Spec coverage check:** every numbered section of the design spec (§1-9)
  maps to at least one task — §1-2 to Tasks 7-8 (route/validation), §3 to
  Tasks 1/3, §4 to Tasks 5-6, §5 to Task 10 (plus Tasks 5-6's rollup
  reasoning), §6 to Task 8's `test_status_never_exposes_the_cleanup_sub_job`,
  §7 to Task 9, §8 to Tasks 8/10's test lists, §9 (naming/prompt) resolved
  by the user before this plan was written and reflected in Global
  Constraints.
- **Type consistency check:** `cleanup_service.process`'s signature
  (`session, redis_client, sub_job_id`) matches every other operation's
  `process` function exactly, and `app/workers/cleanup.py::process_task`
  follows `app/workers/background.py::process_task`'s exact shape including
  the `(status, job_id)` tuple return, which is new here (every other
  worker task returns only `status`) — flagged explicitly in Task 6's
  interface note since a plan-follower skimming `background.py` as a
  template would otherwise miss it.
