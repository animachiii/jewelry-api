"""Supabase Storage upload/download/signed URLs.

Path convention (docs/schema.md): {job_id}/{angle}/{kind}_{short_uuid}.{ext}
Enforced here via `build_storage_path` rather than string-formatted at call
sites. All buckets are private — there is no public read path.

NOT YET LIVE-VERIFIED: Phase 0 Checkpoint 3 requires round-tripping a real
upload against actual Supabase Storage buckets, which needs a Supabase
project + SUPABASE_SERVICE_KEY that do not exist yet in this environment.
The client construction and calls below follow the supabase-py API exactly,
but have not been exercised against a live project.
"""

import tempfile
import uuid
from pathlib import Path
from typing import Any

from supabase import Client, create_client

from app.config import settings
from app.db.models.enums import AssetKind

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)
    return _client


def build_storage_path(job_id: uuid.UUID, angle: str, kind: AssetKind, ext: str) -> str:
    """{job_id}/{angle}/{kind}_{short_uuid}.{ext} — the one place this format is built."""
    short_uuid = uuid.uuid4().hex[:8]
    return f"{job_id}/{angle}/{kind.value.lower()}_{short_uuid}.{ext.lstrip('.')}"


def generate_upload_url(bucket: str, storage_path: str) -> dict[str, Any]:
    """Returns a short-lived signed URL the client can PUT a file to directly."""
    result = get_client().storage.from_(bucket).create_signed_upload_url(storage_path)
    return dict(result)


def generate_signed_url(bucket: str, storage_path: str, ttl_seconds: int | None = None) -> str:
    """Fresh signed read URL, generated on demand — never persisted to the database."""
    ttl = ttl_seconds or settings.SIGNED_URL_TTL_SECONDS
    result = get_client().storage.from_(bucket).create_signed_url(storage_path, ttl)
    signed_url = result.get("signedURL")
    if not signed_url:
        raise RuntimeError(f"Supabase did not return a signedURL for {bucket}/{storage_path}")
    return str(signed_url)


def download_to_temp(bucket: str, storage_path: str) -> Path:
    data = get_client().storage.from_(bucket).download(storage_path)
    suffix = Path(storage_path).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        return Path(tmp.name)


def upload_from_temp(bucket: str, storage_path: str, local_path: Path, content_type: str) -> None:
    with open(local_path, "rb") as f:
        get_client().storage.from_(bucket).upload(
            storage_path, f.read(), {"content-type": content_type}
        )


def exists(bucket: str, storage_path: str) -> bool:
    parent = str(Path(storage_path).parent)
    filename = Path(storage_path).name
    listing = get_client().storage.from_(bucket).list(parent)
    return any(item.get("name") == filename for item in listing)
