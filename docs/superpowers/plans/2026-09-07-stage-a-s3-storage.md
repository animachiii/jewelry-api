# Stage A — S3 Storage Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Supabase Storage with Amazon S3 in both services, so that
object storage no longer depends on Supabase — while both services are still
running on Render.

**Architecture:** Two repositories, two different shapes of change. In V2
(`~/jewelry-api`) all Supabase coupling lives in one module,
`app/services/storage_service.py`; its eleven module-level functions keep their
exact signatures while their internals move to boto3, so none of the thirteen
call sites change. In V1 (`~/Claude/Projects/jewellery-gen-backend`) the
`StorageAdapter` Protocol already exists with a factory and three backends, so
S3 is a fourth backend added alongside them, with no change outside
`app/storage/`.

**Tech Stack:** Python 3.12, boto3/botocore, moto (test double for S3), pytest,
FastAPI, Celery (V2), ARQ (V1).

**Spec:** `docs/superpowers/specs/2026-09-07-client-aws-migration-design.md`

## Global Constraints

- **Both repos:** Python 3.12. Ruff `line-length = 100`.
- **V1 only:** mypy `strict = true`. Dependencies are **pinned to exact
  versions** (`boto3==1.35.99`, `moto==5.0.28`) — matching every other entry in
  its `pyproject.toml`. Test runner: `pytest`.
- **V2 only:** dependencies use **floors** (`boto3>=1.35`), dev deps live under
  `[dependency-groups] dev`. Package manager is `uv`. Test runner: `pytest`.
- **Buckets are private.** Neither service may generate a public-read URL. Every
  client-facing URL is a time-limited presigned URL.
- **Credentials come from the environment/instance profile.** Never construct a
  boto3 client with hardcoded `aws_access_key_id` / `aws_secret_access_key`.
  boto3's default credential chain covers env vars locally and the EC2 instance
  profile in production.
- **Retry discipline (both repos):** retry only failures where **no HTTP
  response was received** (connection reset, timeout). Never retry a real error
  response (`NoSuchKey`, `AccessDenied`, `404`, `403`). Retrying a
  deterministic failure silently swallows it. This rule is why `_with_retries`
  exists in V2 and `_retry_supabase_call` in V1; both are preserved, not removed.
- **No behaviour change is in scope.** Same signatures, same path conventions,
  same response shapes. If a test has to change to accommodate the new backend,
  that is a signal to re-read this constraint.
- Commit messages follow Conventional Commits (`feat:`, `fix:`, `test:`,
  `docs:`, `chore:`).

---

## File Structure

### V2 — `~/jewelry-api`

| File | Responsibility | Change |
|---|---|---|
| `app/config.py` | The only place that reads the environment | Modify: add S3 fields, remove Supabase fields |
| `app/services/storage_service.py` | All object-storage I/O | Modify: internals to boto3, signatures unchanged |
| `tests/unit/test_storage_service.py` | Unit tests for the module | Modify: fakes become moto-backed |
| `tests/conftest.py` | Shared fixtures incl. upload tracking/cleanup | Modify: moto server fixture |
| `pyproject.toml` | Deps | Modify: `+boto3`, `+moto`, `-supabase` |

### V1 — `~/Claude/Projects/jewellery-gen-backend`

| File | Responsibility | Change |
|---|---|---|
| `app/storage/s3.py` | S3 `StorageAdapter` + its client Protocol | **Create** |
| `app/storage/factory.py` | Resolves the configured adapter | Modify: add `s3` branch |
| `app/config.py` | Settings | Modify: add `s3` to the Literal, add fields + validator |
| `tests/test_storage_s3.py` | Unit tests for the new backend | **Create** |
| `pyproject.toml` | Deps | Modify: `+boto3`, `+moto` |

`app/storage/base.py`, `local.py`, `drive.py`, `supabase.py` are **not touched**
— the Protocol's promise is that adding a backend requires no change to the
others.

---

## Task 1: V2 — boto3 client and S3 configuration

**Files:**
- Modify: `~/jewelry-api/pyproject.toml`
- Modify: `~/jewelry-api/app/config.py:24-38`
- Modify: `~/jewelry-api/app/services/storage_service.py:1-90`
- Test: `~/jewelry-api/tests/unit/test_storage_service.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `settings.S3_REGION: str`, `settings.S3_ENDPOINT_URL: str | None`,
    `settings.BUCKET_INPUTS: str`, `settings.BUCKET_OUTPUTS: str` (the last two
    already exist and keep their names and defaults).
  - `storage_service.get_client() -> S3Client` — a boto3 S3 client, cached in
    the module-level `_client` global exactly as today.
  - `storage_service._with_retries[T](operation: str, call: Callable[[], T]) -> T`
    — same signature as today, retrying botocore transport errors.
  - `storage_service.TRANSIENT_ERRORS: tuple[type[Exception], ...]` — the
    exception classes `_with_retries` retries. Tasks 2-5 rely on this name.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add to `dependencies` (keep the list alphabetically
adjacent to existing entries; do **not** remove `supabase` yet — Task 6 does
that, after every function is migrated):

```toml
    "boto3>=1.35",
```

and to `[dependency-groups] dev`:

```toml
    "moto[s3]>=5.0",
```

Then: `cd ~/jewelry-api && uv sync`

- [ ] **Step 2: Write the failing test for the client factory**

Add to `tests/unit/test_storage_service.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k get_client -v`
Expected: FAIL — `AttributeError: S3_REGION` / `'Client' object has no attribute 'meta'`
(the current `get_client` returns a Supabase client).

- [ ] **Step 4: Add the S3 settings fields**

In `app/config.py`, replace the `# --- Supabase Storage ---` block's first two
lines. Leave `BUCKET_INPUTS`, `BUCKET_OUTPUTS`, `SIGNED_URL_TTL_SECONDS`,
`STORAGE_MAX_ATTEMPTS` and `STORAGE_RETRY_BACKOFF_SECONDS` exactly as they are —
bucket names are already S3-shaped and the retry knobs still apply.

```python
    # --- S3 object storage ---
    # Credentials come from boto3's default chain: environment variables
    # locally, the EC2 instance profile in production. Never set explicitly.
    S3_REGION: str = "ap-south-1"
    # Set only to point at a non-AWS S3 endpoint — the moto server in tests,
    # or MinIO in local docker-compose. None means real AWS.
    S3_ENDPOINT_URL: str | None = None
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_KEY: str = ""
```

(`SUPABASE_URL` / `SUPABASE_SERVICE_KEY` stay for now so the app still imports
while Tasks 2-5 are in progress; Task 6 deletes them.)

- [ ] **Step 5: Rewrite the client factory and the retry classes**

In `app/services/storage_service.py`, replace the `from supabase import ...`
import and the `get_client` / `_with_retries` definitions:

```python
import boto3
from botocore.client import Config
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from mypy_boto3_s3.client import S3Client  # type: ignore[import-not-found]
```

If `mypy_boto3_s3` is not installed, type `get_client`'s return as `Any` rather
than adding a stubs dependency — it is not worth a new package here.

```python
_client: S3Client | None = None
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


def get_client() -> S3Client:
    global _client
    if _client is None:
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
            ),
        )
    return _client
```

Then change `_with_retries`'s two `httpx.TransportError` references to
`TRANSIENT_ERRORS`, and its `last_exc` annotation from
`httpx.TransportError | None` to `Exception | None`. Leave the logging call,
the backoff arithmetic and the trailing `assert last_exc is not None` untouched.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k get_client -v`
Expected: PASS (2 passed)

- [ ] **Step 7: Update the existing retry tests to raise a botocore error**

The retry tests currently raise `httpx.ReadTimeout`. Change each to raise
botocore's equivalent, keeping every assertion identical:

```python
from botocore.exceptions import ClientError, ReadTimeoutError

# in place of `raise httpx.ReadTimeout("boom")`:
raise ReadTimeoutError(endpoint_url="https://s3.example.com")
```

Add one test proving the non-retry half of the rule:

```python
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
```

- [ ] **Step 8: Run the full unit test file**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -v`
Expected: every retry test PASSES. Tests calling `download_bytes` against the
Supabase fake will FAIL — that is expected and Task 2 fixes them. Note which
ones fail before continuing.

- [ ] **Step 9: Commit**

```bash
cd ~/jewelry-api
git add pyproject.toml uv.lock app/config.py app/services/storage_service.py tests/unit/test_storage_service.py
git commit -m "feat(storage): boto3 client and botocore retry classification

The retry boundary keeps its exact discipline — transport failures only,
never a real error response — with botocore's internal retries disabled so
_with_retries remains the single tunable policy."
```

---

## Task 2: V2 — downloads on S3

**Files:**
- Modify: `~/jewelry-api/app/services/storage_service.py` (`download_to_temp`, `download_bytes`)
- Test: `~/jewelry-api/tests/unit/test_storage_service.py`

**Interfaces:**
- Consumes: `get_client()`, `_with_retries`, `TRANSIENT_ERRORS` from Task 1.
- Produces: `download_bytes(bucket: str, storage_path: str) -> bytes` and
  `download_to_temp(bucket: str, storage_path: str) -> Path` — signatures
  unchanged from today.

- [ ] **Step 1: Add a moto-backed bucket fixture**

Replace the `_FakeBucket`/`_FakeStorage`/`_FakeClient` classes and the
`fake_client` fixture in `tests/unit/test_storage_service.py` with a real
S3 double. moto uses genuine botocore serialization and signing, so the tests
exercise the same code path production does — the same reasoning that replaced
hand-written Gemini fixtures with the real SDK serializer in August.

```python
import boto3
import pytest
from moto import mock_aws

from app.config import settings
from app.services import storage_service

TEST_BUCKET = "test-bucket"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch):
    """A live-behaving in-memory S3 with TEST_BUCKET created and
    storage_service pointed at it."""
    with mock_aws():
        monkeypatch.setattr(settings, "S3_REGION", "us-east-1")
        monkeypatch.setattr(settings, "S3_ENDPOINT_URL", None)
        monkeypatch.setattr(storage_service, "_client", None)
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=TEST_BUCKET)
        yield client
    monkeypatch.setattr(storage_service, "_client", None)
```

- [ ] **Step 2: Write the failing download tests**

```python
def test_download_bytes_returns_the_stored_object(s3) -> None:
    s3.put_object(Bucket=TEST_BUCKET, Key="a/b/input_1.jpg", Body=b"real-image-bytes")

    assert storage_service.download_bytes(TEST_BUCKET, "a/b/input_1.jpg") == b"real-image-bytes"


def test_download_to_temp_writes_the_bytes_and_keeps_the_suffix(s3) -> None:
    s3.put_object(Bucket=TEST_BUCKET, Key="a/b/input_1.png", Body=b"png-bytes")

    path = storage_service.download_to_temp(TEST_BUCKET, "a/b/input_1.png")

    assert path.suffix == ".png"
    assert path.read_bytes() == b"png-bytes"


def test_download_bytes_propagates_a_missing_key(s3) -> None:
    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        storage_service.download_bytes(TEST_BUCKET, "a/b/nope.jpg")
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k download -v`
Expected: FAIL — the Supabase client is still being constructed.

- [ ] **Step 4: Implement the downloads**

Replace both function bodies. Keep every existing comment — the note about only
the network call being retried, and the note about `download_bytes` existing to
avoid buffering the same object twice (found during the 2026-08-13 OOM
investigation), are both still true and still load-bearing.

```python
def download_to_temp(bucket: str, storage_path: str) -> Path:
    # Only the network call is retried — the temp-file write is local disk
    # I/O, not a Storage call, and must run exactly once per successful
    # download regardless of how many network attempts it took.
    def _download() -> bytes:
        response = get_client().get_object(Bucket=bucket, Key=storage_path)
        return bytes(response["Body"].read())

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

    def _download() -> bytes:
        response = get_client().get_object(Bucket=bucket, Key=storage_path)
        return bytes(response["Body"].read())

    return _with_retries("download", _download)
```

- [ ] **Step 5: Run to verify they pass**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k download -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
cd ~/jewelry-api
git add app/services/storage_service.py tests/unit/test_storage_service.py
git commit -m "feat(storage): download objects from S3

Tests move from a hand-written Supabase fake to moto, so they exercise real
botocore serialization rather than the shape we assumed."
```

---

## Task 3: V2 — uploads on S3

**Files:**
- Modify: `~/jewelry-api/app/services/storage_service.py` (`upload_from_temp`, `upload_bytes`)
- Test: `~/jewelry-api/tests/unit/test_storage_service.py`

**Interfaces:**
- Consumes: `get_client()`, `_with_retries` (Task 1); the `s3` fixture (Task 2).
- Produces: `upload_bytes(bucket: str, storage_path: str, data: bytes, content_type: str) -> None`
  and `upload_from_temp(bucket: str, storage_path: str, local_path: Path, content_type: str) -> None`
  — signatures unchanged.

- [ ] **Step 1: Write the failing upload tests**

```python
def test_upload_bytes_stores_data_and_content_type(s3) -> None:
    storage_service.upload_bytes(TEST_BUCKET, "a/b/out_1.jpg", b"jpeg-bytes", "image/jpeg")

    obj = s3.get_object(Bucket=TEST_BUCKET, Key="a/b/out_1.jpg")
    assert obj["Body"].read() == b"jpeg-bytes"
    assert obj["ContentType"] == "image/jpeg"


def test_upload_from_temp_reads_the_file_once(s3, tmp_path) -> None:
    local = tmp_path / "src.png"
    local.write_bytes(b"png-bytes")

    storage_service.upload_from_temp(TEST_BUCKET, "a/b/out_1.png", local, "image/png")

    obj = s3.get_object(Bucket=TEST_BUCKET, Key="a/b/out_1.png")
    assert obj["Body"].read() == b"png-bytes"
    assert obj["ContentType"] == "image/png"


def test_upload_from_temp_survives_the_caller_deleting_the_file(s3, tmp_path) -> None:
    """The file is read once, outside the retry loop, so a caller that
    deletes local_path immediately after the call cannot race a retry."""
    local = tmp_path / "src.jpg"
    local.write_bytes(b"jpeg-bytes")

    storage_service.upload_from_temp(TEST_BUCKET, "a/b/out_2.jpg", local, "image/jpeg")
    local.unlink()

    assert s3.get_object(Bucket=TEST_BUCKET, Key="a/b/out_2.jpg")["Body"].read() == b"jpeg-bytes"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k upload -v`
Expected: FAIL

- [ ] **Step 3: Implement the uploads**

```python
def upload_from_temp(bucket: str, storage_path: str, local_path: Path, content_type: str) -> None:
    # Read once, outside the retry loop -- re-reading the same local file on
    # every network attempt would be pointless work and, worse, would race
    # a caller that deletes local_path right after calling this.
    with open(local_path, "rb") as f:
        data = f.read()
    _with_retries(
        "upload",
        lambda: get_client().put_object(
            Bucket=bucket, Key=storage_path, Body=data, ContentType=content_type
        ),
    )


def upload_bytes(bucket: str, storage_path: str, data: bytes, content_type: str) -> None:
    """Same as upload_from_temp but for bytes already in memory — used by the
    generation worker (app/services/generation_service.py) to write a
    provider's output directly, without a temp-file round trip."""
    _with_retries(
        "upload",
        lambda: get_client().put_object(
            Bucket=bucket, Key=storage_path, Body=data, ContentType=content_type
        ),
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k upload -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/jewelry-api
git add app/services/storage_service.py tests/unit/test_storage_service.py
git commit -m "feat(storage): upload objects to S3"
```

---

## Task 4: V2 — existence checks and deletion on S3

**Files:**
- Modify: `~/jewelry-api/app/services/storage_service.py` (`exists`, `delete`)
- Test: `~/jewelry-api/tests/unit/test_storage_service.py`

**Interfaces:**
- Consumes: `get_client()`, `_with_retries` (Task 1); `s3` fixture (Task 2);
  `upload_bytes` (Task 3).
- Produces: `exists(bucket: str, storage_path: str) -> bool` and
  `delete(bucket: str, storage_path: str) -> None` — signatures unchanged.

**Why this is its own task:** `exists` changes strategy, not just SDK. Today it
lists the parent prefix and matches a filename; on S3 that is a `head_object`.
The failure mode to get right is that a 404 must become `False`, while a 403 or
a transport error must still raise — an `exists()` that swallows AccessDenied
as "no" would make the retention worker silently skip real objects.

- [ ] **Step 1: Write the failing tests**

```python
def test_exists_is_true_for_a_stored_object(s3) -> None:
    storage_service.upload_bytes(TEST_BUCKET, "a/b/out_1.jpg", b"x", "image/jpeg")

    assert storage_service.exists(TEST_BUCKET, "a/b/out_1.jpg") is True


def test_exists_is_false_for_a_missing_object(s3) -> None:
    assert storage_service.exists(TEST_BUCKET, "a/b/missing.jpg") is False


def test_exists_does_not_match_a_prefix_sibling(s3) -> None:
    """head_object is an exact-key check. The previous list-and-match
    implementation could be fooled by a sibling; this must not be."""
    storage_service.upload_bytes(TEST_BUCKET, "a/b/out_1.jpg", b"x", "image/jpeg")

    assert storage_service.exists(TEST_BUCKET, "a/b/out_1.jpg.bak") is False


def test_delete_removes_the_object(s3) -> None:
    storage_service.upload_bytes(TEST_BUCKET, "a/b/out_1.jpg", b"x", "image/jpeg")

    storage_service.delete(TEST_BUCKET, "a/b/out_1.jpg")

    assert storage_service.exists(TEST_BUCKET, "a/b/out_1.jpg") is False


def test_delete_is_idempotent(s3) -> None:
    """Deleting an object that is already gone must not raise — that is what
    makes retrying a delete safe. See the retention worker."""
    storage_service.delete(TEST_BUCKET, "a/b/never-existed.jpg")
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k "exists or delete" -v`
Expected: FAIL

- [ ] **Step 3: Implement**

```python
def exists(bucket: str, storage_path: str) -> bool:
    """Exact-key existence check.

    A 404 means the object is absent and returns False. Every other error
    response propagates: an AccessDenied swallowed as "absent" would make
    the retention worker silently skip objects it cannot see, which is worse
    than failing loudly.
    """

    def _head() -> bool:
        try:
            get_client().head_object(Bucket=bucket, Key=storage_path)
        except ClientError as exc:
            if exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return False
            raise
        return True

    return _with_retries("head", _head)


def delete(bucket: str, storage_path: str) -> None:
    """Removes bytes for a single object. Idempotent — deleting an object
    that is already gone does not raise, which is also what makes retrying
    it safe (S3's DeleteObject returns 204 for an absent key). Used by the
    retention worker (app/workers/retention.py); never deletes the Asset row
    itself.
    """
    _with_retries("delete", lambda: get_client().delete_object(Bucket=bucket, Key=storage_path))
```

Add `ClientError` to the botocore imports at the top of the module:

```python
from botocore.exceptions import ClientError
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k "exists or delete" -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd ~/jewelry-api
git add app/services/storage_service.py tests/unit/test_storage_service.py
git commit -m "feat(storage): exact-key exists() and idempotent delete() on S3

exists() moves from list-and-match to head_object. A 404 is False; a 403
still raises, so the retention worker cannot silently skip objects it lacks
permission to see."
```

---

## Task 5: V2 — presigned URLs

**Files:**
- Modify: `~/jewelry-api/app/services/storage_service.py` (`generate_signed_url`, `generate_upload_url`)
- Test: `~/jewelry-api/tests/unit/test_storage_service.py`
- Read for context: `~/jewelry-api/app/api/v2/uploads.py`

**Interfaces:**
- Consumes: `get_client()`, `_with_retries` (Task 1); `s3` fixture (Task 2).
- Produces:
  - `generate_signed_url(bucket: str, storage_path: str, ttl_seconds: int | None = None) -> str`
  - `generate_upload_url(bucket: str, storage_path: str) -> dict[str, Any]` —
    **the returned dict must contain the key `signedUrl`**.

**Why the dict key matters.** `app/api/v2/uploads.py` reads
`result["signedUrl"]` at **six** call sites (operation, background, mask,
secondary, secondary-mask, and the per-angle loop). That camelCase key is a
Supabase artifact, but it is now part of this module's contract with its only
caller. Keeping it means `uploads.py` needs no change; the alternative is
editing six call sites for no behavioural gain. Keep the key.

Note also that today's `generate_signed_url` reads `result.get("signedURL")` —
capital `URL`, a different casing from `signedUrl` above, because Supabase's
two endpoints disagree. That inconsistency disappears here: `generate_signed_url`
now builds and returns the string directly.

- [ ] **Step 1: Write the failing tests**

```python
def test_generate_signed_url_round_trips_with_httpx(s3) -> None:
    """A presigned GET must actually fetch the object. Asserting the URL
    merely contains 'Signature' would pass for a URL that 403s."""
    import httpx

    storage_service.upload_bytes(TEST_BUCKET, "a/b/out_1.jpg", b"jpeg-bytes", "image/jpeg")

    url = storage_service.generate_signed_url(TEST_BUCKET, "a/b/out_1.jpg")

    assert httpx.get(url).content == b"jpeg-bytes"


def test_generate_signed_url_honours_an_explicit_ttl(s3) -> None:
    storage_service.upload_bytes(TEST_BUCKET, "a/b/out_1.jpg", b"x", "image/jpeg")

    url = storage_service.generate_signed_url(TEST_BUCKET, "a/b/out_1.jpg", ttl_seconds=60)

    assert "X-Amz-Expires=60" in url


def test_generate_signed_url_defaults_to_the_configured_ttl(s3, monkeypatch) -> None:
    monkeypatch.setattr(settings, "SIGNED_URL_TTL_SECONDS", 1800)
    storage_service.upload_bytes(TEST_BUCKET, "a/b/out_1.jpg", b"x", "image/jpeg")

    url = storage_service.generate_signed_url(TEST_BUCKET, "a/b/out_1.jpg")

    assert "X-Amz-Expires=1800" in url


def test_generate_upload_url_returns_a_signedUrl_key(s3) -> None:
    """app/api/v2/uploads.py reads result["signedUrl"] at six call sites.
    That key is this module's contract with its caller."""
    result = storage_service.generate_upload_url(TEST_BUCKET, "a/b/input_1.jpg")

    assert "signedUrl" in result
    assert result["signedUrl"].startswith("http")


def test_generate_upload_url_round_trips_with_httpx(s3) -> None:
    """A presigned PUT must actually accept an upload."""
    import httpx

    result = storage_service.generate_upload_url(TEST_BUCKET, "a/b/input_1.jpg")

    response = httpx.put(result["signedUrl"], content=b"uploaded-bytes")

    assert response.status_code == 200
    assert storage_service.download_bytes(TEST_BUCKET, "a/b/input_1.jpg") == b"uploaded-bytes"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k signed_url -v`
Expected: FAIL

- [ ] **Step 3: Implement**

```python
# Matches the Supabase upload-URL lifetime this replaces, and
# app/api/v2/uploads.py's own _UPLOAD_URL_TTL_SECONDS, which stamps the
# expires_at the client is shown. Keep the two in step.
_UPLOAD_URL_TTL_SECONDS = 600


def generate_upload_url(bucket: str, storage_path: str) -> dict[str, Any]:
    """Returns a short-lived presigned URL the client can PUT a file to directly.

    The `signedUrl` key is inherited from the Supabase implementation this
    replaces and is read at six call sites in app/api/v2/uploads.py — it is
    this function's contract with its caller, not an accident.
    """
    url = _with_retries(
        "generate_upload_url",
        lambda: get_client().generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": storage_path},
            ExpiresIn=_UPLOAD_URL_TTL_SECONDS,
        ),
    )
    return {"signedUrl": url, "path": storage_path}


def generate_signed_url(bucket: str, storage_path: str, ttl_seconds: int | None = None) -> str:
    """Fresh signed read URL, generated on demand — never persisted to the database."""
    ttl = ttl_seconds or settings.SIGNED_URL_TTL_SECONDS
    return _with_retries(
        "generate_signed_url",
        lambda: get_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": storage_path},
            ExpiresIn=ttl,
        ),
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -k signed_url -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run the whole unit file**

Run: `cd ~/jewelry-api && uv run pytest tests/unit/test_storage_service.py -v`
Expected: PASS, all tests.

- [ ] **Step 6: Commit**

```bash
cd ~/jewelry-api
git add app/services/storage_service.py tests/unit/test_storage_service.py
git commit -m "feat(storage): presigned GET and PUT URLs

Keeps the signedUrl dict key, which app/api/v2/uploads.py reads at six call
sites. Tests round-trip the URLs over real HTTP rather than pattern-matching
the query string."
```

---

## Task 6: V2 — remove Supabase, and take CI off the live service

**Files:**
- Modify: `~/jewelry-api/pyproject.toml`
- Modify: `~/jewelry-api/app/config.py`
- Modify: `~/jewelry-api/app/services/storage_service.py` (module docstring)
- Modify: `~/jewelry-api/tests/conftest.py`
- Modify: `~/jewelry-api/docs/deployment-free-tier.md`, `~/jewelry-api/CLAUDE.md`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: a repository with no `supabase` import and no `SUPABASE_*` setting.

**Why the conftest change is part of this task.** Integration tests currently
upload **real bytes to real Supabase**, which is why CI needed live credentials
and why five unrelated tests failed in one week on network blips to Supabase.
Pointing them at a `moto` server instead removes both problems at once. This
belongs here rather than in a later task because the moment `supabase` leaves
`pyproject.toml`, those tests cannot run any other way.

- [ ] **Step 1: Add a session-scoped moto server fixture**

In `tests/conftest.py`:

```python
import boto3
import pytest
from moto.server import ThreadedMotoServer

from app.config import settings


@pytest.fixture(scope="session", autouse=True)
def _moto_s3_server() -> Iterator[None]:
    """Integration tests upload real bytes. Before the S3 migration those
    bytes went to a live Supabase project, which needed credentials in CI
    and made the suite hostage to network blips — five unrelated tests
    failed that way in one week (see storage_service.py's docstring). A
    moto server is a real HTTP S3 endpoint, so presigned-URL round trips
    still work, with no external dependency.

    Session-scoped: one server for the run. Note tests/integration/ also
    shares a session-scoped Postgres container, and the same rule applies
    here — a test that mutates shared state must reset it on the way out.
    """
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"

    settings.S3_ENDPOINT_URL = endpoint
    settings.S3_REGION = "us-east-1"

    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    for bucket in (settings.BUCKET_INPUTS, settings.BUCKET_OUTPUTS):
        client.create_bucket(Bucket=bucket)

    yield

    server.stop()
```

`ThreadedMotoServer` needs credentials present in the environment; if the suite
runs without any, set `AWS_ACCESS_KEY_ID=testing` and
`AWS_SECRET_ACCESS_KEY=testing` in the fixture via `monkeypatch` at session
scope, or in `pyproject.toml`'s `[tool.pytest.ini_options] env`.

- [ ] **Step 2: Run the full suite**

Run: `cd ~/jewelry-api && uv run pytest -v`
Expected: PASS. Investigate every failure before proceeding — a test that only
passes against live Supabase is a test that was never testing this code.

- [ ] **Step 3: Delete the Supabase settings and dependency**

In `app/config.py`, delete the `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` lines
added back in Task 1 Step 4.

In `pyproject.toml`, delete `"supabase>=2.9",`. Then `uv sync`.

- [ ] **Step 4: Prove nothing references Supabase**

Run:

```bash
cd ~/jewelry-api && grep -rn "supabase\|SUPABASE\|storage3" app tests pyproject.toml
```

Expected: no matches in code. Matches inside historical comments in
`docs/` are fine and should stay — they are the record of why the retry
discipline exists.

- [ ] **Step 5: Update the module docstring**

`app/services/storage_service.py`'s docstring opens with "Supabase Storage
upload/download/signed URLs." Rewrite the first line and the "NOT YET
LIVE-VERIFIED" paragraph (which describes a Supabase project that no longer
exists). **Keep the entire 2026-08-28 retry paragraph**, adding a sentence that
the classification now maps onto botocore: it explains a rule that still
governs this module, and deleting it would lose the reason.

- [ ] **Step 6: Update the docs and env table**

In `docs/deployment-free-tier.md` and `CLAUDE.md`, replace `SUPABASE_URL` /
`SUPABASE_SERVICE_KEY` in the env-var tables with `S3_REGION` and
`S3_ENDPOINT_URL`, noting that credentials come from the instance profile and
are deliberately not env vars.

- [ ] **Step 7: Run the full suite, linter and type checker**

```bash
cd ~/jewelry-api && uv run pytest -v && uv run ruff check . && uv run mypy app
```
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
cd ~/jewelry-api
git add -A
git commit -m "feat(storage)!: remove Supabase Storage in favour of S3

Integration tests now run against a moto server instead of a live Supabase
project, which also removes the network-blip class of CI flake documented in
storage_service.py."
```

---

## Task 7: V1 — S3 client behind the existing Protocol pattern

**Files:**
- Create: `~/Claude/Projects/jewellery-gen-backend/app/storage/s3.py`
- Create: `~/Claude/Projects/jewellery-gen-backend/tests/test_storage_s3.py`
- Modify: `~/Claude/Projects/jewellery-gen-backend/pyproject.toml`
- Read for context: `app/storage/supabase.py`, `app/storage/base.py`

**Interfaces:**
- Consumes: `app.worker.retry.DEFAULT_DELAYS` (existing).
- Produces, in `app/storage/s3.py`:
  - `class S3ApiError(Exception)` with `.status: int`
  - `class S3StorageError(Exception)`
  - `class S3Client(Protocol)` with async `upload(bucket, key, data, mime) -> None`,
    `download(bucket, key) -> tuple[bytes, str]`, `exists(bucket, key) -> bool`
  - `class Boto3S3Client` implementing that Protocol

**Why mirror `supabase.py` rather than call boto3 directly.** `app/storage/`'s
established shape is a narrow async Protocol with a real implementation and a
fake used by tests. Following it means `S3Storage` (Task 8) is testable without
any AWS surface at all, exactly as `SupabaseStorage` is today.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, add to `dependencies` (exact pins, matching every other
entry in this repo):

```toml
    "boto3==1.35.99",
```

and to `[project.optional-dependencies] dev`:

```toml
    "moto[s3]==5.0.28",
```

Add a mypy override, since boto3 ships no stubs and this repo is `strict`:

```toml
[[tool.mypy.overrides]]
module = ["boto3.*", "botocore.*"]
ignore_missing_imports = true
```

Then: `cd ~/Claude/Projects/jewellery-gen-backend && pip install -e ".[dev]"`

- [ ] **Step 2: Write the failing test for the real client**

Create `tests/test_storage_s3.py`:

```python
"""app/storage/s3.py — the S3 StorageAdapter and its client.

Boto3S3Client is exercised against moto (real botocore serialization and
signing); S3Storage is exercised against FakeS3Client, mirroring how
tests/test_storage_supabase.py separates the two concerns.
"""

import boto3
import pytest
from moto import mock_aws

from app.storage.s3 import Boto3S3Client, S3ApiError

BUCKET = "v1-test-bucket"


@pytest.fixture
def s3_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.mark.asyncio
async def test_upload_then_download_round_trips_data_and_mime(s3_bucket) -> None:
    client = Boto3S3Client(region="us-east-1")

    await client.upload(BUCKET, "ref123", b"image-bytes", "image/jpeg")
    data, mime = await client.download(BUCKET, "ref123")

    assert data == b"image-bytes"
    assert mime == "image/jpeg"


@pytest.mark.asyncio
async def test_exists_reflects_presence(s3_bucket) -> None:
    client = Boto3S3Client(region="us-east-1")

    assert await client.exists(BUCKET, "ref123") is False
    await client.upload(BUCKET, "ref123", b"x", "image/jpeg")
    assert await client.exists(BUCKET, "ref123") is True


@pytest.mark.asyncio
async def test_download_of_a_missing_key_raises_S3ApiError_404(s3_bucket) -> None:
    client = Boto3S3Client(region="us-east-1")

    with pytest.raises(S3ApiError) as excinfo:
        await client.download(BUCKET, "nope")

    assert excinfo.value.status == 404


@pytest.mark.asyncio
async def test_error_messages_never_leak_a_url_or_credential(s3_bucket) -> None:
    """Same rule SupabaseApiError carries: the message must not include a
    raw object URL or any credential."""
    client = Boto3S3Client(region="us-east-1")

    with pytest.raises(S3ApiError) as excinfo:
        await client.download(BUCKET, "nope")

    assert "http" not in str(excinfo.value).lower()
    assert BUCKET not in str(excinfo.value)
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd ~/Claude/Projects/jewellery-gen-backend && pytest tests/test_storage_s3.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.storage.s3'`

- [ ] **Step 4: Implement the client**

Create `app/storage/s3.py`:

```python
"""S3-backed StorageAdapter.

Mirrors app/storage/supabase.py's shape exactly: a narrow async Protocol
(S3Client) with one real implementation (Boto3S3Client) and a fake used by
tests, so S3Storage itself is testable with no AWS surface at all.

boto3 is synchronous. Every call is wrapped in asyncio.to_thread rather than
introducing an async S3 library, matching app/storage/local.py, which does
the same for blocking filesystem I/O.

Credentials come from boto3's default chain — environment variables locally,
the EC2 instance profile in production. Never passed explicitly.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar
from uuid import uuid4

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.worker.retry import DEFAULT_DELAYS

T = TypeVar("T")


class S3ApiError(Exception):
    """Raised by an S3Client implementation (real or fake) for any S3 API
    failure, carrying the HTTP status so S3Storage can classify retryable
    vs. terminal failures. Message must never include a credential or a raw
    object URL — mirrors SupabaseApiError."""

    def __init__(self, status: int, message: str = "S3 API error") -> None:
        self.status = status
        super().__init__(message)


class S3StorageError(Exception):
    """Raised when an S3 operation fails terminally: retries exhausted, or a
    definite non-retryable error (404, 400, non-quota auth failure)."""


def _is_retryable(exc: S3ApiError) -> bool:
    # 503 SlowDown is S3's throttle response; 5xx is transient server error.
    # Everything else (400, 403, 404) is terminal.
    return exc.status == 429 or exc.status >= 500


class S3Client(Protocol):
    """Async S3 surface. The real implementation wraps boto3;
    FakeS3Client in tests is the only other implementation."""

    async def upload(self, bucket: str, key: str, data: bytes, mime: str) -> None: ...

    async def download(self, bucket: str, key: str) -> tuple[bytes, str]: ...

    async def exists(self, bucket: str, key: str) -> bool: ...


class Boto3S3Client:
    def __init__(self, region: str, endpoint_url: str | None = None) -> None:
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            # Retries are owned by _retry_s3_call below, the single tested
            # policy. Two nested retry layers would multiply attempts.
            config=Config(
                signature_version="s3v4",
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )

    @staticmethod
    def _status(exc: ClientError) -> int:
        return int(exc.response["ResponseMetadata"]["HTTPStatusCode"])

    async def upload(self, bucket: str, key: str, data: bytes, mime: str) -> None:
        def _put() -> None:
            try:
                self._client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=mime)
            except ClientError as exc:
                raise S3ApiError(self._status(exc)) from exc

        await asyncio.to_thread(_put)

    async def download(self, bucket: str, key: str) -> tuple[bytes, str]:
        def _get() -> tuple[bytes, str]:
            try:
                response = self._client.get_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                raise S3ApiError(self._status(exc)) from exc
            mime = str(response.get("ContentType", "application/octet-stream"))
            return bytes(response["Body"].read()), mime

        return await asyncio.to_thread(_get)

    async def exists(self, bucket: str, key: str) -> bool:
        def _head() -> bool:
            try:
                self._client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if self._status(exc) == 404:
                    return False
                raise S3ApiError(self._status(exc)) from exc
            return True

        return await asyncio.to_thread(_head)


async def _retry_s3_call(
    call: Callable[[], Awaitable[T]], *, delays: tuple[float, ...] = DEFAULT_DELAYS
) -> T:
    last_exc: S3ApiError | None = None
    for attempt in range(len(delays) + 1):
        try:
            return await call()
        except S3ApiError as exc:
            if not _is_retryable(exc):
                raise S3StorageError("S3 operation failed.") from exc
            last_exc = exc
            if attempt < len(delays):
                await asyncio.sleep(delays[attempt])
    assert last_exc is not None
    raise S3StorageError("S3 operation failed after retries.") from last_exc
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd ~/Claude/Projects/jewellery-gen-backend && pytest tests/test_storage_s3.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
cd ~/Claude/Projects/jewellery-gen-backend
git add app/storage/s3.py tests/test_storage_s3.py pyproject.toml
git commit -m "feat(storage): S3 client behind the storage Protocol

Mirrors app/storage/supabase.py — narrow async Protocol, one real boto3
implementation, retry classification on HTTP status."
```

---

## Task 8: V1 — S3Storage adapter and factory wiring

**Files:**
- Modify: `~/Claude/Projects/jewellery-gen-backend/app/storage/s3.py` (append `S3Storage`)
- Modify: `~/Claude/Projects/jewellery-gen-backend/app/storage/factory.py`
- Modify: `~/Claude/Projects/jewellery-gen-backend/app/config.py:99-105, 201-215`
- Modify: `~/Claude/Projects/jewellery-gen-backend/tests/test_storage_s3.py`

**Interfaces:**
- Consumes: `S3Client`, `S3ApiError`, `S3StorageError`, `_retry_s3_call`,
  `DEFAULT_DELAYS` from Task 7.
- Produces:
  - `class S3Storage` implementing `StorageAdapter` — `put(data, filename, mime) -> str`,
    `get(ref) -> tuple[bytes, str]`, `exists(ref) -> bool`
  - `settings.storage_backend` accepts `"s3"`; `settings.s3_bucket: str | None`;
    `settings.aws_region: str`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage_s3.py`:

```python
from app.storage.s3 import S3Storage, S3StorageError


class FakeS3Client:
    """The only S3Client implementation besides Boto3S3Client. Mirrors
    tests/test_storage_supabase.py's FakeSupabaseStorageClient."""

    def __init__(self, fail_times: int = 0, status: int = 500) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.upload_calls = 0
        self._fail_times = fail_times
        self._status = status

    async def upload(self, bucket: str, key: str, data: bytes, mime: str) -> None:
        self.upload_calls += 1
        if self.upload_calls <= self._fail_times:
            raise S3ApiError(self._status)
        self.objects[key] = (data, mime)

    async def download(self, bucket: str, key: str) -> tuple[bytes, str]:
        if key not in self.objects:
            raise S3ApiError(404)
        return self.objects[key]

    async def exists(self, bucket: str, key: str) -> bool:
        return key in self.objects


@pytest.mark.asyncio
async def test_put_returns_an_opaque_ref_with_no_path_or_filename() -> None:
    storage = S3Storage(FakeS3Client(), BUCKET)

    ref = await storage.put(b"bytes", "client-supplied-name.jpg", "image/jpeg")

    assert "/" not in ref
    assert "client-supplied-name" not in ref
    assert len(ref) == 32


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    storage = S3Storage(FakeS3Client(), BUCKET)

    ref = await storage.put(b"bytes", "x.jpg", "image/jpeg")

    assert await storage.get(ref) == (b"bytes", "image/jpeg")


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried() -> None:
    client = FakeS3Client(fail_times=1, status=500)
    storage = S3Storage(client, BUCKET, retry_delays=(0.0,))

    ref = await storage.put(b"bytes", "x.jpg", "image/jpeg")

    assert client.upload_calls == 2
    assert await storage.exists(ref) is True


@pytest.mark.asyncio
async def test_a_terminal_failure_is_not_retried() -> None:
    client = FakeS3Client(fail_times=1, status=403)
    storage = S3Storage(client, BUCKET, retry_delays=(0.0,))

    with pytest.raises(S3StorageError):
        await storage.put(b"bytes", "x.jpg", "image/jpeg")

    assert client.upload_calls == 1, "a 403 must not be retried"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd ~/Claude/Projects/jewellery-gen-backend && pytest tests/test_storage_s3.py -v`
Expected: FAIL — `ImportError: cannot import name 'S3Storage'`

- [ ] **Step 3: Implement `S3Storage`**

Append to `app/storage/s3.py`:

```python
class S3Storage:
    """S3-backed StorageAdapter. Objects go into `bucket`; the generated
    uuid4 hex key is used directly as the opaque storage_ref — same scheme
    SupabaseStorage uses, so refs are interchangeable in shape and nothing
    outside app/storage/ can tell the backends apart."""

    def __init__(
        self,
        client: S3Client,
        bucket: str,
        *,
        retry_delays: tuple[float, ...] = DEFAULT_DELAYS,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._retry_delays = retry_delays

    async def put(self, data: bytes, filename: str, mime: str) -> str:
        ref = uuid4().hex

        async def _do() -> str:
            await self._client.upload(self._bucket, ref, data, mime)
            return ref

        return await _retry_s3_call(_do, delays=self._retry_delays)

    async def get(self, ref: str) -> tuple[bytes, str]:
        async def _do() -> tuple[bytes, str]:
            return await self._client.download(self._bucket, ref)

        return await _retry_s3_call(_do, delays=self._retry_delays)

    async def exists(self, ref: str) -> bool:
        async def _do() -> bool:
            return await self._client.exists(self._bucket, ref)

        return await _retry_s3_call(_do, delays=self._retry_delays)
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd ~/Claude/Projects/jewellery-gen-backend && pytest tests/test_storage_s3.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Add the settings**

In `app/config.py`, extend the Literal on line 99 and add fields near the other
storage settings:

```python
    storage_backend: Literal["local", "drive", "supabase", "s3"] = Field(
```

```python
    s3_bucket: str | None = Field(default=None, alias="S3_BUCKET")
    aws_region: str = Field(default="ap-south-1", alias="AWS_REGION")
```

And a validator alongside `_require_supabase_config_when_selected`:

```python
    @model_validator(mode="after")
    def _require_s3_config_when_selected(self) -> "Settings":
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        return self
```

Match the exact decorator, naming and raise style of the existing
`_require_supabase_config_when_selected` — read it first.

- [ ] **Step 6: Write the failing factory test**

Add to `tests/test_storage_s3.py`:

```python
def test_factory_resolves_s3_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings
    from app.storage.factory import get_storage_adapter

    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", BUCKET)
    monkeypatch.setattr(settings, "aws_region", "us-east-1")

    adapter = get_storage_adapter()

    assert isinstance(adapter, S3Storage)
```

Run it: `pytest tests/test_storage_s3.py::test_factory_resolves_s3_backend -v`
Expected: FAIL — the factory falls through to `LocalStorage`.

- [ ] **Step 7: Add the factory branch**

In `app/storage/factory.py`, add before the final `return LocalStorage()`, and
extend the docstring's list of backends to mention `s3`:

```python
    if settings.storage_backend == "s3":
        from app.storage.s3 import Boto3S3Client, S3Storage

        assert settings.s3_bucket is not None
        return S3Storage(Boto3S3Client(settings.aws_region), settings.s3_bucket)
```

- [ ] **Step 8: Run the full suite, linter and type checker**

```bash
cd ~/Claude/Projects/jewellery-gen-backend
pytest -v && ruff check . && mypy app
```
Expected: all PASS. `mypy --strict` must be clean — if boto3 types complain,
verify the override added in Task 7 Step 1 is present and correctly scoped.

- [ ] **Step 9: Update the deployment docs**

In `render.yaml`'s comment block and `docs/deployment-free-tier.md`, note that
`STORAGE_BACKEND=s3` requires `S3_BUCKET` and `AWS_REGION`, and that credentials
come from the environment or instance profile. Do not delete the Supabase rows —
that backend still exists and still works.

- [ ] **Step 10: Commit**

```bash
cd ~/Claude/Projects/jewellery-gen-backend
git add -A
git commit -m "feat(storage): S3 storage adapter selectable via STORAGE_BACKEND=s3

Fourth backend alongside local/drive/supabase, with no change to any code
outside app/storage/ — the Protocol's stated promise."
```

---

## Task 9: Stage A live verification

**Files:** none — this is a verification gate, run by the user against the
deployed services.

**Interfaces:**
- Consumes: Tasks 1-8 deployed to Render.

**Why this task exists.** Both services now pass their suites against moto.
moto is not S3: it does not enforce IAM, and its presigned-URL validation is
more forgiving than the real service. Nothing is proven until real bytes land
in a real bucket through a real presigned URL.

- [ ] **Step 1: Create the buckets**

In the client's AWS account, in the chosen region, create three **private**
buckets with public access fully blocked: `jewelry-inputs`, `jewelry-outputs`,
and one for V1 matching `S3_BUCKET`.

- [ ] **Step 2: Create a deploy-time IAM user for Render**

Render has no instance profile, so this interim stage needs an access key —
scoped to `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` on
those three buckets and nothing else. **This key is temporary and is deleted at
the end of Stage C**, when the EC2 instance profile replaces it. Record it as
an item to revoke.

- [ ] **Step 3: Set the environment variables in the Render dashboard**

For `jewelry-api`: `S3_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`;
remove `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.
For `jewellery-gen-backend`: `STORAGE_BACKEND=s3`, `S3_BUCKET`, `AWS_REGION`,
and the same key pair.

In edit mode Render's secret fields render as **empty** (`data-state="hidden"`)
— that is masking, not loss. Do not re-enter a value that looks blank.

- [ ] **Step 4: Deploy and confirm health**

Both services redeploy on push. Confirm
`GET https://jewelry-api-qc8b.onrender.com/api/v2/health` reports
`storage: ok`, and V1's `/health` is green.

Health is a necessary check, not the gate — the 512 MB instance also reported
healthy between OOM kills.

- [ ] **Step 5: Run the real V2 round trip**

Through the `/ui` page (job submission needs an `X-API-Key` that lives in that
page's `localStorage`):

1. Presign an upload, PUT a real 3072×4096 photo to the returned URL.
2. Confirm the object appears in `jewelry-inputs` in the S3 console.
3. Submit a BACKGROUND_REMOVAL job; wait for a terminal status.
4. Download the output through its presigned URL and **open it in an image
   viewer.**

Step 5.4 is the actual gate. A COMPLETED status and a clean download prove
nothing on their own: the August base64 bug produced a file of exactly the
right length, entirely wrong content, a COMPLETED sub-job, and a download that
simply would not open.

- [ ] **Step 6: Byte-compare a passthrough**

Download the *input* you uploaded in Step 5.1 back through
`generate_signed_url` and confirm it is byte-identical to the local original
(`shasum` both). This isolates storage correctness from generation.

- [ ] **Step 7: Run the V1 round trip**

Submit one V1 generation end to end and confirm the output is retrievable and
opens.

- [ ] **Step 8: Confirm the retention path**

Trigger or wait for the retention worker and confirm a deleted object is
actually gone from the bucket — `delete` and `exists` are the two functions with
no coverage from Steps 5-7.

- [ ] **Step 9: Record the result**

Append a short "Stage A verified" note to the spec's §6, with the date, the job
ids used, and anything that behaved differently against real S3 than against
moto. Commit it.

**Do not begin Stage B until every step above passes.** The value of the staged
sequencing is entirely in not carrying an unverified change into the next one.

---

## Self-Review Notes

Checked against the spec's §"Object storage: Supabase Storage → S3":

- Buckets stay private, presigned URLs only — Task 9 Step 1, Tasks 5 and 7.
- Instance profile, not access keys — Global Constraints; the temporary Render
  key in Task 9 Step 2 is the documented exception, with its revocation
  recorded.
- V1 as a fourth backend with no change outside `app/storage/` — Tasks 7-8;
  verified by Task 8 Step 8's full suite.
- V2's eleven functions keep their signatures — Tasks 2-5; the `signedUrl` key
  is called out explicitly in Task 5 because six call sites depend on it.
- `_with_retries` preserved with its catch narrowed to transport errors —
  Task 1 Steps 5 and 7, with an explicit non-retry test.
- `build_storage_path` unchanged — no task touches it, correctly.
- VPC endpoint and S3 gateway routing are **Stage C**, not this plan.

Two things this plan adds beyond the spec, both deliberate: the moto server
fixture in Task 6 (which removes the live-Supabase dependency from CI, closing
a documented flake) and the temporary Render IAM key in Task 9 (unavoidable
while Stage A runs on Render, and explicitly scheduled for deletion).
