"""POST /api/v2/internal/config/sync business logic. See docs/api-routes.md and
docs/business-rules.md §9.

Pipeline: fetch raw Sheets rows -> normalize into the `config_versions.payload`
shape (docs/schema.md) -> SHA-256 hash -> write a new immutable row only if the
hash changed -> activate it -> invalidate the Redis `config:active` cache.

Sheets outages never fail the sync: `SheetsUnavailableError` (see
`app/providers/sheets.py`) falls back to returning the currently active version
unchanged, with nothing written — there is no new payload to record, so there is
nothing to mark FAILED. A malformed-but-fetched payload is a different case: it
*is* recorded, as `sync_status: FAILED`, and the previous version stays active —
this is what docs/business-rules.md §9 means by "a sync that fails validation."
"""

import hashlib
import json
from collections.abc import Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.config_versions import ConfigVersion
from app.db.models.enums import Angle, SyncStatus
from app.db.repositories import config_versions as config_versions_repo
from app.providers.sheets import SheetRows, SheetsUnavailableError, fetch_sheet_rows
from app.services.config_service import invalidate_cache

logger = structlog.get_logger()

_VALID_ANGLES = {angle.value for angle in Angle}


class ConfigValidationError(ValueError):
    """Raised by `normalize_sheet_rows` when the fetched payload is malformed."""


def normalize_sheet_rows(
    rows: SheetRows, inherited_global: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Pure transform: raw Sheets rows -> the `config_versions.payload` shape
    (docs/schema.md). Deterministic ordering (sorted by category code, then
    angle) so `compute_source_hash` is stable regardless of row order in the
    sheet — an ops author reordering rows without changing content must not
    create a spurious new version.

    `inherited_global` is the currently-active version's `global` block, used as
    the base that any global rows in the sheet override. The client's real sheet
    has no Global tab at all (see `app/providers/sheets.py`), so without this
    every real sync would emit a `global` containing only the three keys this
    function knows how to build — silently dropping `unit_cost_usd`, which
    `generation_service` reads on every call and whose absence previously
    crashed every real `/generate` (fixed by hand in Phase 13; this keeps that
    fix from being undone by the first real sync).

    Raises `ConfigValidationError` on any structurally invalid row.
    """
    categories: dict[str, dict[str, Any]] = {}
    for row in rows.angle_rows:
        if len(row) < 5:
            raise ConfigValidationError(f"Angle row has fewer than 5 columns: {row!r}")
        code, name, is_active_raw, angle, enabled_raw, *rest = row
        synthetic_allowed_raw = rest[0] if len(rest) > 0 else "FALSE"
        prompt = rest[1] if len(rest) > 1 and rest[1] != "" else None
        negative_prompt = rest[2] if len(rest) > 2 and rest[2] != "" else None
        reference_image_urls_raw = rest[3] if len(rest) > 3 else ""

        if not code:
            raise ConfigValidationError("Angle row missing category_code")
        if angle not in _VALID_ANGLES:
            raise ConfigValidationError(f"Unknown angle {angle!r} for category {code!r}")

        category = categories.setdefault(
            code,
            {
                "code": code,
                "name": name,
                "is_active": _parse_bool(is_active_raw),
                "angles": {},
            },
        )
        if angle in category["angles"]:
            raise ConfigValidationError(f"Duplicate angle {angle!r} for category {code!r}")

        angle_spec: dict[str, Any] = {
            "enabled": _parse_bool(enabled_raw),
            "synthetic_allowed": _parse_bool(synthetic_allowed_raw),
            "prompt": prompt,
            "reference_image_urls": (
                [url.strip() for url in reference_image_urls_raw.split(",") if url.strip()]
                if reference_image_urls_raw
                else []
            ),
        }
        if negative_prompt is not None:
            angle_spec["negative_prompt"] = negative_prompt
        category["angles"][angle] = angle_spec

    if not categories:
        raise ConfigValidationError("No category rows found")

    for category in categories.values():
        missing = _VALID_ANGLES - category["angles"].keys()
        for angle in missing:
            category["angles"][angle] = {
                "enabled": False,
                "synthetic_allowed": False,
                "prompt": None,
                "reference_image_urls": [],
            }

    base_global: dict[str, Any] = dict(inherited_global or {})
    global_kv = {key: value for key, value in rows.global_rows if key}

    model_version = global_kv.get("model_version") or base_global.get("model_version")
    if not model_version:
        raise ConfigValidationError(
            "No model_version: absent from both the sheet and the active config version"
        )

    raw_threshold = global_kv.get("qa_similarity_threshold")
    if raw_threshold is not None:
        try:
            qa_threshold = float(raw_threshold)
        except ValueError as exc:
            raise ConfigValidationError("qa_similarity_threshold is not a number") from exc
    else:
        qa_threshold = float(base_global.get("qa_similarity_threshold", 0.82))
    if not 0.0 <= qa_threshold <= 1.0:
        raise ConfigValidationError("qa_similarity_threshold must be between 0 and 1")

    # Start from the inherited block so keys this function doesn't model
    # (notably unit_cost_usd) survive a sync, then apply what the sheet says.
    merged_global = base_global
    merged_global["model_version"] = model_version
    merged_global["qa_similarity_threshold"] = qa_threshold
    merged_global["default_negative_prompt"] = global_kv.get(
        "default_negative_prompt", base_global.get("default_negative_prompt", "")
    )

    return {
        "categories": [categories[code] for code in sorted(categories)],
        "global": merged_global,
    }


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().upper() in {"TRUE", "1", "YES"}


def compute_source_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the normalized payload — docs/business-rules.md §9:
    "Unchanged hash creates no new version." `sort_keys=True` makes the hash
    stable across dict insertion order.
    """
    canonical = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def sync_config(
    session: AsyncSession,
    redis: Redis,
    fetch_rows: Callable[[], SheetRows] = fetch_sheet_rows,
) -> ConfigVersion:
    """Orchestrates the full sync. Commits internally — this is a standalone
    top-level action, not composed with any other transaction (contrast
    `job_service.create_job_for_request`, which does not commit because the
    route calls it alone as its own unit of work — same rule, same result here).
    """
    try:
        rows = fetch_rows()
    except SheetsUnavailableError:
        logger.warning("config_sync_sheets_unavailable")
        active = await config_versions_repo.get_active(session)
        if active is None:
            raise
        return active

    active = await config_versions_repo.get_active(session)

    try:
        inherited_global = dict(active.payload.get("global", {})) if active is not None else None
        payload = normalize_sheet_rows(rows, inherited_global)
    except ConfigValidationError as exc:
        logger.warning("config_sync_validation_failed", error=str(exc))
        version_number = await config_versions_repo.get_next_version_number(session)
        # Best-effort hash of the raw rows so a repeated identical bad sync doesn't
        # need special-casing — it still creates one FAILED row per attempt, which
        # is correct: each attempt is evidence for ops to look at.
        raw_hash = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        await config_versions_repo.create_version(
            session,
            version_number=version_number,
            source_hash=raw_hash,
            payload={"categories": [], "global": {}},
            sync_status=SyncStatus.FAILED,
            error_message=str(exc),
        )
        await session.commit()
        if active is None:
            raise
        return active

    new_hash = compute_source_hash(payload)
    if active is not None and active.source_hash == new_hash:
        logger.info("config_sync_unchanged", version_number=active.version_number)
        return active

    version_number = await config_versions_repo.get_next_version_number(session)
    version = await config_versions_repo.create_version(
        session,
        version_number=version_number,
        source_hash=new_hash,
        payload=payload,
        sync_status=SyncStatus.SUCCESS,
    )
    await config_versions_repo.activate_version(session, version)
    await session.commit()
    await invalidate_cache(redis)
    logger.info("config_sync_activated", version_number=version.version_number)
    return version
