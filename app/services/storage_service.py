"""Supabase Storage upload/download/signed URLs.

Path convention (docs/schema.md): {job_id}/{angle}/{kind}_{short_uuid}.{ext}
Enforced here via `build_storage_path` rather than string-formatted at call
sites. All buckets are private — there is no public read path.

NOT YET LIVE-VERIFIED: Phase 0 Checkpoint 3 requires round-tripping a real
upload against actual Supabase Storage buckets, which needs a Supabase
project + SUPABASE_SERVICE_KEY that do not exist yet in this environment.
The client construction and calls below follow the supabase-py API exactly,
but have not been exercised against a live project.

**2026-08-28 — every call below retries on a transient network failure.**
Found via a very visible symptom: `test_api_mix.py`/`test_api_recolor.py`/
`test_rate_limit_quota.py` (and others — a different, unrelated test each
time) failed in CI five times in one week, always with the identical
signature: `httpcore.ReadTimeout` deep inside `storage3`'s `httpx` call,
surfacing as a client-visible `500 INTERNAL_ERROR`. Every failure passed
immediately when the specific test was re-run in isolation, and Supabase
itself answered in well under a second when queried directly — this is
GitHub Actions' network egress to Supabase stalling mid-read, not a Supabase
outage and not a bug in the code being tested. The identical failure mode is
just as real against production traffic, not only CI: any request path that
touches Storage (`image_validation.inspect_and_validate` on every
`/generate`/`/background/*`/`/match`/`/recolor`/`/mix` call, plus every
worker's own download/upload) was one blip away from a spurious 500.

`_with_retries` catches only `httpx.TransportError` — timeouts, connection
resets, protocol errors: failures where no HTTP response was ever received.
A real Supabase error response (object not found, bad auth, bad request)
raises `storage3.utils.StorageException` instead, an unrelated exception
type that is never caught here and propagates on the first attempt, same as
before this change. Retrying that would be silently swallowing a real,
deterministic failure — this must never do that.
"""

import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
import structlog
from botocore.client import Config
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)
from botocore.exceptions import (
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.config import settings
from app.db.models.enums import AssetKind

_client: Any | None = None
_logger = structlog.get_logger()

# Transport-level failures only: no HTTP response was ever received. A real
# S3 error response (NoSuchKey, AccessDenied) raises botocore's ClientError,
# which is deliberately NOT in this tuple and propagates on the first
# attempt. Retrying a deterministic failure silently swallows it. This
# mirrors exactly what httpx.TransportError covered before the S3 move.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    BotoConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
    EndpointConnectionError,
)


def get_client() -> Any:
    global _client
    if _client is None:
        s3_addressing: dict[str, Any] = {}
        if settings.S3_ENDPOINT_URL is not None:
            # Path-style addressing (bucket/key in the path, not
            # bucket.host in the hostname). Virtual-hosted-style — boto3's
            # default — needs DNS resolution of a subdomain that only
            # exists for real AWS endpoints; a local moto server or MinIO
            # has no such DNS entry, so a non-None endpoint always means a
            # non-AWS target that needs path style. Real AWS
            # (S3_ENDPOINT_URL is None) never takes this branch and keeps
            # boto3's default addressing.
            s3_addressing["s3"] = {"addressing_style": "path"}
        _client = boto3.client(
            "s3",
            region_name=settings.S3_REGION,
            endpoint_url=settings.S3_ENDPOINT_URL,
            # total_max_attempts=1 means "try once, never retry internally".
            # _with_retries below is the single, tested retry boundary; two
            # nested retry policies would multiply attempts and make the
            # backoff untunable.
            config=Config(
                signature_version="s3v4",
                retries={"total_max_attempts": 1, "mode": "standard"},
                **s3_addressing,
            ),
        )
    return _client


def _with_retries[T](operation: str, call: Callable[[], T]) -> T:
    """Runs `call`, retrying only on transport-level failures — see this
    module's own docstring for exactly what that does and does not cover.
    `operation` is a short label (e.g. "download", "upload") for the log
    line on a retried attempt; nothing structural depends on its value.
    """
    last_exc: Exception | None = None
    for attempt in range(1, settings.STORAGE_MAX_ATTEMPTS + 1):
        try:
            return call()
        except TRANSIENT_ERRORS as exc:
            last_exc = exc
            if attempt == settings.STORAGE_MAX_ATTEMPTS:
                raise
            # docs/conventions.md's own logging table names "transient
            # retry" as a warning-level example.
            _logger.warning(
                "storage_transient_retry",
                operation=operation,
                attempt=attempt,
                max_attempts=settings.STORAGE_MAX_ATTEMPTS,
                error=str(exc),
            )
            time.sleep(settings.STORAGE_RETRY_BACKOFF_SECONDS * attempt)
    # Unreachable: the loop above always either returns or raises on the
    # final attempt. Satisfies mypy --strict, which cannot see that.
    assert last_exc is not None
    raise last_exc


def build_storage_path(job_id: uuid.UUID, angle: str, kind: AssetKind, ext: str) -> str:
    """{job_id}/{angle}/{kind}_{short_uuid}.{ext} — the one place this format is built."""
    short_uuid = uuid.uuid4().hex[:8]
    return f"{job_id}/{angle}/{kind.value.lower()}_{short_uuid}.{ext.lstrip('.')}"


def generate_upload_url(bucket: str, storage_path: str) -> dict[str, Any]:
    """Returns a short-lived signed URL the client can PUT a file to directly."""
    result = _with_retries(
        "generate_upload_url",
        lambda: get_client().storage.from_(bucket).create_signed_upload_url(storage_path),
    )
    return dict(result)


def generate_signed_url(bucket: str, storage_path: str, ttl_seconds: int | None = None) -> str:
    """Fresh signed read URL, generated on demand — never persisted to the database."""
    ttl = ttl_seconds or settings.SIGNED_URL_TTL_SECONDS
    result = _with_retries(
        "generate_signed_url",
        lambda: get_client().storage.from_(bucket).create_signed_url(storage_path, ttl),
    )
    signed_url = result.get("signedURL")
    if not signed_url:
        raise RuntimeError(f"Supabase did not return a signedURL for {bucket}/{storage_path}")
    return str(signed_url)


def download_to_temp(bucket: str, storage_path: str) -> Path:
    # Only the network call is retried — the temp-file write is local disk
    # I/O, not a Storage call, and must run exactly once per successful
    # download regardless of how many network attempts it took.
    def _download() -> bytes:
        return bytes(get_client().storage.from_(bucket).download(storage_path))

    data = _with_retries("download", _download)
    suffix = Path(storage_path).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        return Path(tmp.name)


def download_bytes(bucket: str, storage_path: str) -> bytes:
    """Same download as download_to_temp, without the write-then-reread
    round trip through a temp file — for callers that only need the bytes
    (every caller except image_validation.py, which needs a real path for
    PIL). Found during the 2026-08-13 BACKGROUND_REMOVAL OOM investigation:
    download_to_temp().read_bytes() buffers the same object in memory twice.
    """
    return bytes(
        _with_retries("download", lambda: get_client().storage.from_(bucket).download(storage_path))
    )


def upload_from_temp(bucket: str, storage_path: str, local_path: Path, content_type: str) -> None:
    # Read once, outside the retry loop -- re-reading the same local file on
    # every network attempt would be pointless work and, worse, would race
    # a caller that deletes local_path right after calling this.
    with open(local_path, "rb") as f:
        data = f.read()
    _with_retries(
        "upload",
        lambda: (
            get_client()
            .storage.from_(bucket)
            .upload(storage_path, data, {"content-type": content_type})
        ),
    )


def upload_bytes(bucket: str, storage_path: str, data: bytes, content_type: str) -> None:
    """Same as upload_from_temp but for bytes already in memory — used by the
    generation worker (app/services/generation_service.py) to write a
    provider's output directly, without a temp-file round trip."""
    _with_retries(
        "upload",
        lambda: (
            get_client()
            .storage.from_(bucket)
            .upload(storage_path, data, {"content-type": content_type})
        ),
    )


def exists(bucket: str, storage_path: str) -> bool:
    parent = str(Path(storage_path).parent)
    filename = Path(storage_path).name
    listing = _with_retries("list", lambda: get_client().storage.from_(bucket).list(parent))
    return any(item.get("name") == filename for item in listing)


def delete(bucket: str, storage_path: str) -> None:
    """Removes bytes for a single object. Idempotent — deleting an object
    that is already gone does not raise, which is also what makes retrying
    it safe. Used by the retention worker (app/workers/retention.py); never
    deletes the Asset row itself.
    """
    _with_retries("delete", lambda: get_client().storage.from_(bucket).remove([storage_path]))
