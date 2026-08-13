"""check_qa_retry_preconditions (app/services/job_service.py) and
execute_qa_retry (app/services/retry_service.py) — retrying only the QA
judge call on a flagged background-operation sub-job, without re-running
generation. See docs/superpowers/specs/2026-08-13-background-qa-preview-and-retry-design.md.
"""

import uuid

import pytest

from app.db.models.enums import QAStatus, SourceType, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.services.job_service import (
    MAX_RETRY_ATTEMPTS,
    RetryLimitExceededError,
    SubJobNotRetryableError,
    check_qa_retry_preconditions,
)
from app.services.retry_service import execute_qa_retry


def _sub_job(**overrides: object) -> SubJob:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "job_id": uuid.uuid4(),
        "angle": None,
        "status": SubJobStatus.QA_REVIEW,
        "qa_status": QAStatus.FLAGGED,
        "source_type": SourceType.UPLOADED,
        "attempt_count": 0,
    }
    defaults.update(overrides)
    return SubJob(**defaults)  # type: ignore[arg-type]


def test_qa_review_flagged_is_retryable() -> None:
    check_qa_retry_preconditions(_sub_job())  # must not raise


def test_failed_sub_job_is_not_qa_retryable() -> None:
    """FAILED goes through the existing check_retry_preconditions path
    (a full regenerate), not this one."""
    with pytest.raises(SubJobNotRetryableError):
        check_qa_retry_preconditions(_sub_job(status=SubJobStatus.FAILED))


def test_qa_review_but_not_flagged_is_not_qa_retryable() -> None:
    """QA_REVIEW with qa_status PASSED shouldn't be reachable (that
    combination completes instead), but pin it as rejected rather than
    silently accepted."""
    with pytest.raises(SubJobNotRetryableError):
        check_qa_retry_preconditions(_sub_job(qa_status=QAStatus.PASSED))


def test_completed_sub_job_is_not_qa_retryable() -> None:
    with pytest.raises(SubJobNotRetryableError):
        check_qa_retry_preconditions(_sub_job(status=SubJobStatus.COMPLETED))


def test_at_retry_ceiling_raises_limit_exceeded() -> None:
    with pytest.raises(RetryLimitExceededError):
        check_qa_retry_preconditions(_sub_job(attempt_count=MAX_RETRY_ATTEMPTS))


def test_below_retry_ceiling_is_retryable() -> None:
    check_qa_retry_preconditions(_sub_job(attempt_count=MAX_RETRY_ATTEMPTS - 1))


class _FakeSession:
    """record_event only ever calls session.add() -- no real DB needed to
    unit-test execute_qa_retry's own logic (attempt_count, status)."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


def test_execute_qa_retry_increments_attempt_count_and_leaves_status_untouched() -> None:
    job = Job(id=uuid.uuid4())
    sub_job = _sub_job(attempt_count=1)
    session = _FakeSession()

    execute_qa_retry(session, job, sub_job)  # type: ignore[arg-type]

    assert sub_job.attempt_count == 2
    # No regeneration: status/qa_status stay exactly as they were until the
    # re-dispatched qa.score_background call itself overwrites them.
    assert sub_job.status == SubJobStatus.QA_REVIEW
    assert sub_job.qa_status == QAStatus.FLAGGED


def test_execute_qa_retry_records_a_job_event() -> None:
    job = Job(id=uuid.uuid4())
    sub_job = _sub_job()
    session = _FakeSession()

    execute_qa_retry(session, job, sub_job)  # type: ignore[arg-type]

    assert len(session.added) == 1
    assert session.added[0].event_type == "QA_RETRY_REQUESTED"
