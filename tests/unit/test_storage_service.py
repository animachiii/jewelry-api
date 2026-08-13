"""app/services/storage_service.py::download_bytes.

Found during the 2026-08-13 BACKGROUND_REMOVAL OOM investigation:
download_to_temp() downloads the object into memory, writes it to a temp
file, and returns the path -- every caller that only wants bytes then calls
.read_bytes() on that path, buffering the same object in memory twice.
download_bytes() returns the client's bytes directly, no temp file.
"""

from typing import Any

import pytest

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


def test_download_bytes_returns_the_objects_bytes(fake_client: Any) -> None:
    result = storage_service.download_bytes("jewelry-inputs", "job/1/input.jpg")

    assert result == b"real-image-bytes"


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
