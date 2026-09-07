"""app/services/storage_service.py::download_bytes/download_to_temp and the
retry wrapper.

Found during the 2026-08-13 BACKGROUND_REMOVAL OOM investigation:
download_to_temp() downloads the object into memory, writes it to a temp
file, and returns the path -- every caller that only wants bytes then calls
.read_bytes() on that path, buffering the same object in memory twice.
download_bytes() returns the client's bytes directly, no temp file.

2026-08-28: also covers _with_retries, added after the identical
httpx.ReadTimeout signature failed a different, unrelated test in CI five
times in one week -- see storage_service.py's own module docstring.

2026-09-07 (Task 2, S3 migration): download_to_temp/download_bytes now call
get_client().get_object(...) instead of the old Supabase-style
`.storage.from_(bucket).download(...)`. Their tests below moved from a
hand-written Supabase fake to a real local S3 server (moto's
ThreadedMotoServer, not mock_aws() -- see the `s3` fixture's own docstring
for why a real listening socket matters for a later task's presigned-URL
tests). upload_bytes below is still Task 3/4/5's Supabase-style
implementation, unmodified here, so its own retry test keeps the old fake
client it always used.
"""

from typing import Any

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError, ConnectTimeoutError
from moto.server import ThreadedMotoServer

from app.config import settings
from app.services import storage_service

TEST_BUCKET = "test-bucket"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real local S3-compatible HTTP server (moto), with TEST_BUCKET
    created and storage_service pointed at it. A real server, not
    mock_aws()'s pure transport interception, because Task 5's tests hit
    presigned URLs with raw httpx -- see this step's note above.
    """
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"

    monkeypatch.setattr(settings, "S3_REGION", "us-east-1")
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", endpoint)
    monkeypatch.setattr(storage_service, "_client", None)

    # Unlike moto's mock_aws() (which patches botocore's transport and never
    # signs a real request), ThreadedMotoServer is a real HTTP server, so
    # botocore's SigV4 signer runs for real and raises NoCredentialsError if
    # no credentials are resolvable -- confirmed in this environment, which
    # has no AWS credential chain configured at all. Any value works; moto's
    # backend doesn't check them.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    client.create_bucket(Bucket=TEST_BUCKET)

    yield client

    server.stop()
    monkeypatch.setattr(storage_service, "_client", None)


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every retry test below exercises real sleep() calls between attempts
    -- zero the backoff so the suite doesn't pay for it in wall-clock time.
    """
    monkeypatch.setattr(settings, "STORAGE_RETRY_BACKOFF_SECONDS", 0.0)


def test_download_bytes_returns_the_stored_object(s3: Any) -> None:
    s3.put_object(Bucket=TEST_BUCKET, Key="a/b/input_1.jpg", Body=b"real-image-bytes")

    assert storage_service.download_bytes(TEST_BUCKET, "a/b/input_1.jpg") == b"real-image-bytes"


def test_download_to_temp_writes_the_bytes_and_keeps_the_suffix(s3: Any) -> None:
    s3.put_object(Bucket=TEST_BUCKET, Key="a/b/input_1.png", Body=b"png-bytes")

    path = storage_service.download_to_temp(TEST_BUCKET, "a/b/input_1.png")

    assert path.suffix == ".png"
    assert path.read_bytes() == b"png-bytes"


def test_download_bytes_propagates_a_missing_key(s3: Any) -> None:
    with pytest.raises(ClientError):
        storage_service.download_bytes(TEST_BUCKET, "a/b/nope.jpg")


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


class _FakeBucket:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.download_calls: list[str] = []

    def download(self, storage_path: str) -> bytes:
        self.download_calls.append(storage_path)
        return self._data


class _FakeStorage:
    def __init__(self, bucket: Any) -> None:
        self._bucket = bucket

    def from_(self, bucket_name: str) -> Any:
        return self._bucket


class _FakeClient:
    def __init__(self, data: bytes) -> None:
        self.storage = _FakeStorage(_FakeBucket(data))


def test_upload_bytes_only_reads_source_data_once_across_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upload_bytes's caller-supplied `data` must be the exact same bytes on
    every retried attempt -- there's no local file to accidentally re-read
    here, but this pins that the retry loop doesn't mutate or re-derive it.

    upload_bytes itself is not this task's scope (Tasks 3-5 still own it and
    its Supabase-style call shape) -- this test and its fake client are
    unchanged from before the S3 download rewrite.
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
