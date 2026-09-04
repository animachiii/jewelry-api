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
