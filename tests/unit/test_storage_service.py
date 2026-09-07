"""app/services/storage_service.py::download_bytes and the retry wrapper.

Found during the 2026-08-13 BACKGROUND_REMOVAL OOM investigation:
download_to_temp() downloads the object into memory, writes it to a temp
file, and returns the path -- every caller that only wants bytes then calls
.read_bytes() on that path, buffering the same object in memory twice.
download_bytes() returns the client's bytes directly, no temp file.

2026-08-28: also covers _with_retries, added after the identical
httpx.ReadTimeout signature failed a different, unrelated test in CI five
times in one week -- see storage_service.py's own module docstring.
"""

from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from app.config import settings
from app.services import storage_service


class _FakeBucket:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.download_calls: list[str] = []

    def download(self, storage_path: str) -> bytes:
        self.download_calls.append(storage_path)
        return self._data


class _FakeStorage:
    def __init__(self, bucket: _FakeBucket) -> None:
        self._bucket = bucket

    def from_(self, bucket_name: str) -> _FakeBucket:
        return self._bucket


class _FakeClient:
    def __init__(self, data: bytes) -> None:
        self.storage = _FakeStorage(_FakeBucket(data))


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    client = _FakeClient(b"real-image-bytes")
    monkeypatch.setattr(storage_service, "get_client", lambda: client)
    return client


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every retry test below exercises real sleep() calls between attempts
    -- zero the backoff so the suite doesn't pay for it in wall-clock time.
    """
    monkeypatch.setattr(settings, "STORAGE_RETRY_BACKOFF_SECONDS", 0.0)


def test_download_bytes_returns_the_objects_bytes(fake_client: Any) -> None:
    result = storage_service.download_bytes("jewelry-inputs", "job/1/input.jpg")

    assert result == b"real-image-bytes"


class _FlakyThenOkBucket:
    """Fails with a given exception `fail_times` times, then returns data --
    models the real CI failure signature: the first N attempts hit
    httpcore.ReadTimeout (surfaces as httpx.ReadTimeout, a TransportError
    subclass), then Supabase answers normally, same as when the specific
    failing test was re-run in isolation.
    """

    def __init__(self, data: bytes, fail_times: int, exc: Exception) -> None:
        self._data = data
        self._fail_times = fail_times
        self._exc = exc
        self.call_count = 0

    def download(self, storage_path: str) -> bytes:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise self._exc
        return self._data


def test_download_bytes_retries_a_transient_timeout_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bucket = _FlakyThenOkBucket(
        b"real-image-bytes",
        fail_times=1,
        exc=ReadTimeoutError(endpoint_url="https://s3.example.com"),
    )
    client = _FakeClient(b"unused")
    client.storage = _FakeStorage(bucket)  # type: ignore[assignment]
    monkeypatch.setattr(storage_service, "get_client", lambda: client)

    result = storage_service.download_bytes("jewelry-inputs", "job/1/input.jpg")

    assert result == b"real-image-bytes"
    assert bucket.call_count == 2  # one failure, one success


def test_download_bytes_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient failure on every attempt must still surface to the
    caller -- this is bounded retry, not infinite retry, and not silently
    swallowing a real, persistent outage.
    """
    bucket = _FlakyThenOkBucket(
        b"real-image-bytes",
        fail_times=999,
        exc=ReadTimeoutError(endpoint_url="https://s3.example.com"),
    )
    client = _FakeClient(b"unused")
    client.storage = _FakeStorage(bucket)  # type: ignore[assignment]
    monkeypatch.setattr(storage_service, "get_client", lambda: client)

    with pytest.raises(ReadTimeoutError):
        storage_service.download_bytes("jewelry-inputs", "job/1/input.jpg")

    assert bucket.call_count == settings.STORAGE_MAX_ATTEMPTS


def test_a_real_storage_error_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing safety property: a deterministic Supabase error
    response (object not found, bad auth) raises storage3's own
    StorageException, not an httpx exception, and must propagate on the
    first attempt -- retrying a real error would silently mask it and waste
    STORAGE_MAX_ATTEMPTS worth of latency on something that will never
    succeed.
    """
    from storage3.utils import StorageException

    bucket = _FlakyThenOkBucket(
        b"real-image-bytes", fail_times=999, exc=StorageException("object not found")
    )
    client = _FakeClient(b"unused")
    client.storage = _FakeStorage(bucket)  # type: ignore[assignment]
    monkeypatch.setattr(storage_service, "get_client", lambda: client)

    with pytest.raises(StorageException):
        storage_service.download_bytes("jewelry-inputs", "job/1/input.jpg")

    assert bucket.call_count == 1  # never retried


def test_with_retries_does_not_retry_a_real_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ClientError is S3 answering. Retrying it would swallow a real,
    deterministic failure — see this module's retry discipline."""
    calls = []

    def _boom() -> None:
        calls.append(1)
        raise ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
        )

    with pytest.raises(ClientError):
        storage_service._with_retries("download", _boom)

    assert len(calls) == 1, "a real error response must not be retried"


def test_upload_bytes_only_reads_source_data_once_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upload_bytes's caller-supplied `data` must be the exact same bytes on
    every retried attempt -- there's no local file to accidentally re-read
    here, but this pins that the retry loop doesn't mutate or re-derive it.
    """

    class _FlakyUploadBucket:
        def __init__(self) -> None:
            self.call_count = 0
            self.received: list[bytes] = []

        def upload(self, storage_path: str, data: bytes, options: dict[str, str]) -> None:
            self.call_count += 1
            self.received.append(data)
            if self.call_count == 1:
                raise ConnectTimeoutError(endpoint_url="https://s3.example.com")

    bucket = _FlakyUploadBucket()
    client = _FakeClient(b"unused")
    client.storage = _FakeStorage(bucket)  # type: ignore[assignment]
    monkeypatch.setattr(storage_service, "get_client", lambda: client)

    storage_service.upload_bytes("jewelry-outputs", "job/1/output.png", b"payload", "image/png")

    assert bucket.call_count == 2
    assert bucket.received == [b"payload", b"payload"]


def test_download_bytes_does_not_write_a_temp_file(
    fake_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: download_to_temp's NamedTemporaryFile round trip is
    what caused the double buffering. download_bytes must never touch disk.
    """
    import tempfile

    def _fail_named_temp_file(*args: object, **kwargs: object) -> None:
        raise AssertionError("download_bytes must not create a temp file")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _fail_named_temp_file)

    result = storage_service.download_bytes("jewelry-inputs", "job/1/input.jpg")

    assert result == b"real-image-bytes"


def test_get_client_is_cached_and_targets_configured_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_service, "_client", None)
    monkeypatch.setattr(settings, "S3_REGION", "ap-south-1")

    first = storage_service.get_client()
    second = storage_service.get_client()

    assert first is second, "client must be cached in the module global"
    assert first.meta.region_name == "ap-south-1"


def test_get_client_disables_botocore_internal_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_with_retries owns retry policy. If botocore also retried, a transient
    failure would be attempted STORAGE_MAX_ATTEMPTS x botocore's own count,
    and the retry test assertions below would silently stop meaning anything.
    """
    monkeypatch.setattr(storage_service, "_client", None)
    client = storage_service.get_client()

    assert client.meta.config.retries["total_max_attempts"] == 1
