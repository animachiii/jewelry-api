"""Phase 2 Checkpoint 3 — compute_parent_status against every row of
docs/business-rules.md §3.
"""

import pytest

from app.db.models.enums import JobStatus
from app.services.status_rollup import compute_parent_status


@pytest.mark.parametrize(
    ("requested", "succeeded", "failed", "expected"),
    [
        # S + F < R -> PROCESSING
        (4, 0, 0, JobStatus.PROCESSING),
        (4, 2, 0, JobStatus.PROCESSING),
        (4, 1, 1, JobStatus.PROCESSING),
        # S == R -> COMPLETED
        (4, 4, 0, JobStatus.COMPLETED),
        (2, 2, 0, JobStatus.COMPLETED),
        (1, 1, 0, JobStatus.COMPLETED),
        # F == R -> FAILED
        (4, 0, 4, JobStatus.FAILED),
        (1, 0, 1, JobStatus.FAILED),  # single-angle failure is FAILED, not PARTIAL_SUCCESS
        # S > 0 and F > 0 and S + F == R -> PARTIAL_SUCCESS
        (4, 3, 1, JobStatus.PARTIAL_SUCCESS),
        (4, 1, 3, JobStatus.PARTIAL_SUCCESS),
        (2, 1, 1, JobStatus.PARTIAL_SUCCESS),
    ],
)
def test_compute_parent_status_matches_business_rules_table(
    requested: int, succeeded: int, failed: int, expected: JobStatus
) -> None:
    assert compute_parent_status(requested, succeeded, failed) == expected


def test_single_angle_failure_is_failed_not_partial_success() -> None:
    """The row teams get wrong: partial success requires >=1 success AND >=1 failure."""
    assert compute_parent_status(1, 0, 1) == JobStatus.FAILED
