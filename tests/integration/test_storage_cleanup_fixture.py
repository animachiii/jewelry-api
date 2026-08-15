"""Phase 16 Step 4 regression test — the fix for the storage-pollution bug
found by scripts/audit_storage.py (docs/storage-audit-2026-08.md): test runs
uploading real bytes to the real, shared Supabase project with nothing ever
cleaning them up. Exercises tests/conftest.py's actual
`track_storage_uploads` context manager (the same one the autouse
`_cleanup_storage_uploads` fixture uses), against real Supabase Storage —
matching this repo's own "never mock Storage" testing convention.
"""

import uuid

import pytest

from app.config import settings
from app.services import storage_service
from tests.conftest import track_storage_uploads

pytestmark = pytest.mark.integration


def test_tracked_uploads_are_deleted_on_context_exit() -> None:
    path = f"phase16-fixture-regression-test/{uuid.uuid4()}.bin"

    with track_storage_uploads() as uploaded:
        storage_service.upload_bytes(settings.BUCKET_INPUTS, path, b"x", "application/octet-stream")
        assert (settings.BUCKET_INPUTS, path) in uploaded
        assert storage_service.exists(settings.BUCKET_INPUTS, path)

    assert not storage_service.exists(settings.BUCKET_INPUTS, path)


def test_untracked_uploads_outside_the_context_are_unaffected() -> None:
    """Sanity check that the patch/restore is scoped correctly — an upload
    before or after the context manager is active must not be silently
    swept up or leave storage_service permanently patched.
    """
    orig_upload_bytes = storage_service.upload_bytes

    with track_storage_uploads():
        pass

    assert storage_service.upload_bytes is orig_upload_bytes
