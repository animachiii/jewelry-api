"""Parent-status rollup. See docs/business-rules.md §3.

Pure function, no I/O. Every phase that transitions a sub-job (7, 8, 9) calls
this and persists the result inside the same transaction as the transition —
see docs/conventions.md.
"""

from app.db.models.enums import JobStatus


def compute_parent_status(requested: int, succeeded: int, failed: int) -> JobStatus:
    if succeeded + failed < requested:
        return JobStatus.PROCESSING
    if succeeded == requested:
        return JobStatus.COMPLETED
    if failed == requested:
        return JobStatus.FAILED
    return JobStatus.PARTIAL_SUCCESS
