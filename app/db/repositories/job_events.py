"""Writes to `job_events` — the append-only audit log. See docs/schema.md.

`record_event` adds the row and does not commit; the caller controls the
transaction boundary (see docs/conventions.md — a transition and its audit
row belong in the same transaction as everything else about that change).
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.job_events import JobEvent


def record_event(
    session: AsyncSession,
    job_id: uuid.UUID,
    event_type: str,
    *,
    sub_job_id: uuid.UUID | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: dict[str, Any] | None = None,
) -> JobEvent:
    event = JobEvent(
        job_id=job_id,
        sub_job_id=sub_job_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        detail=detail or {},
    )
    session.add(event)
    return event
