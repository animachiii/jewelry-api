"""Cost event recording. See docs/business-rules.md §10: one row per
provider call, including calls that end in refusal — "a refused generation
is still billed."
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.cost_events import CostEvent


def record_cost_event(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    sub_job_id: uuid.UUID | None,
    provider: str,
    operation: str,
    model_version: str,
    unit_cost_usd: float,
    units: int = 1,
) -> CostEvent:
    """Adds and returns a new CostEvent row. Does not commit — caller
    controls the transaction boundary (see docs/conventions.md).
    `unit_cost_usd` always comes from configuration
    (`config_versions.payload.global.unit_cost_usd`), never a hardcoded
    constant — provider pricing changes and historical rows must retain the
    rate that applied at the time.
    """
    event = CostEvent(
        job_id=job_id,
        sub_job_id=sub_job_id,
        provider=provider,
        operation=operation,
        model_version=model_version,
        units=units,
        unit_cost_usd=unit_cost_usd,
        total_cost_usd=unit_cost_usd * units,
    )
    session.add(event)
    return event
