"""status_service.build_background_result_status — preview_image_url and
retryable/retry_url extended to cover a QA-flagged sub-job. See
docs/superpowers/specs/2026-08-13-background-qa-preview-and-retry-design.md.
image_url itself must stay exactly as documented: COMPLETED-only, untouched
by this change.
"""

import uuid

import pytest

from app.db.models.enums import FailureClass, QAStatus, SourceType, SubJobStatus
from app.db.models.jobs import Job, SubJob
from app.services import status_service


@pytest.fixture(autouse=True)
def _fake_signed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status_service.storage_service,
        "generate_signed_url",
        lambda bucket, path, ttl_seconds=None: f"https://signed.example/{bucket}/{path}",
    )


def _job() -> Job:
    return Job(id=uuid.uuid4())


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


def test_preview_image_url_present_when_flagged_and_output_exists() -> None:
    result = status_service.build_background_result_status(
        _job(), _sub_job(), ("jewelry-outputs", "job/background/output.jpg")
    )
    assert (
        result.preview_image_url
        == "https://signed.example/jewelry-outputs/job/background/output.jpg"
    )
    # The documented, ERP-facing field is untouched: still COMPLETED-only.
    assert result.image_url is None


def test_preview_image_url_absent_when_no_output_asset_yet() -> None:
    result = status_service.build_background_result_status(
        _job(), _sub_job(status=SubJobStatus.GENERATING, qa_status=QAStatus.NOT_APPLICABLE), None
    )
    assert result.preview_image_url is None


def test_completed_still_populates_both_image_url_and_preview_image_url() -> None:
    result = status_service.build_background_result_status(
        _job(),
        _sub_job(status=SubJobStatus.COMPLETED, qa_status=QAStatus.PASSED),
        ("jewelry-outputs", "job/background/output.jpg"),
    )
    assert result.image_url == result.preview_image_url


def test_flagged_sub_job_is_retryable_via_job_level_retry_url() -> None:
    job = _job()
    result = status_service.build_background_result_status(
        job, _sub_job(), ("jewelry-outputs", "job/background/output.jpg")
    )
    assert result.retryable is True
    assert result.retry_url == f"/api/v2/jobs/{job.id}/retry"


def test_provider_error_flag_qa_score_none_is_still_retryable() -> None:
    """Real production incident, 2026-08-13: the QA judge call itself can
    fail (qa_score stays NULL, sub_job still lands FLAGGED) -- this must be
    just as retryable as a real low score."""
    job = _job()
    result = status_service.build_background_result_status(
        job, _sub_job(qa_score=None), ("jewelry-outputs", "job/background/output.jpg")
    )
    assert result.retryable is True


def test_qa_review_but_not_flagged_is_not_retryable() -> None:
    result = status_service.build_background_result_status(
        _job(),
        _sub_job(qa_status=QAStatus.PASSED),
        ("jewelry-outputs", "job/background/output.jpg"),
    )
    assert result.retryable is False
    assert result.retry_url is None


def test_generating_is_not_retryable() -> None:
    result = status_service.build_background_result_status(
        _job(), _sub_job(status=SubJobStatus.GENERATING, qa_status=QAStatus.NOT_APPLICABLE), None
    )
    assert result.retryable is False


def test_failed_retryable_class_still_uses_the_same_retry_url() -> None:
    """Existing FAILED-retry behavior must be unchanged by this addition."""
    job = _job()
    result = status_service.build_background_result_status(
        job,
        _sub_job(
            status=SubJobStatus.FAILED,
            qa_status=QAStatus.NOT_APPLICABLE,
            failure_class=FailureClass.TRANSIENT_NETWORK,
        ),
        None,
    )
    assert result.retryable is True
    assert result.retry_url == f"/api/v2/jobs/{job.id}/retry"
