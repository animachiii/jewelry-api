"""Structural image validation for uploaded assets (Phase 4).

Deliberately *not* perceptual/content ML validation — see
docs/decisions/0001-drop-local-matting.md, which rules out local ML-based
image processing for this project. This module only verifies that the bytes
a client PUT to Supabase Storage decode as a real, non-corrupt image in a
supported format, and extracts the structural metadata
(`width_px`/`height_px`/`bytes`/`checksum_sha256`) that `assets` rows need.
"""

import hashlib
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from app.core.errors import AppError, ErrorCode
from app.services import storage_service

_SUPPORTED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class InvalidImageError(AppError):
    """Uploaded object is empty, not decodable as an image, or not in a
    supported format. Maps to the existing `VALIDATION_ERROR` code — the
    error taxonomy in docs/api-routes.md is meant to be stable and doesn't
    have a dedicated "corrupt image" code; see
    phases/phase-4-storage-ingest.md Step 2 for the reasoning.
    """

    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


@dataclass(frozen=True)
class ImageMetadata:
    width_px: int
    height_px: int
    bytes: int
    checksum_sha256: str
    mime_type: str


def inspect_and_validate(bucket: str, storage_path: str) -> ImageMetadata:
    """Downloads the object at `storage_path` and validates it is a real,
    decodable image. Raises `InvalidImageError` if not. Never raises for
    missing objects — callers are expected to have already confirmed
    existence (see `storage_service.exists`); a race there would surface as
    a download failure, which is allowed to propagate as-is.
    """
    local_path = storage_service.download_to_temp(bucket, storage_path)
    try:
        data = local_path.read_bytes()
        if len(data) == 0:
            raise InvalidImageError(
                f"Uploaded object at {storage_path} is empty.",
                details={"storage_path": storage_path},
            )

        try:
            with Image.open(local_path) as img:
                img.verify()
            # verify() leaves the file object unusable for further reads —
            # re-open to actually read format/size.
            with Image.open(local_path) as img2:
                fmt = img2.format
                width, height = img2.size
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidImageError(
                f"Uploaded object at {storage_path} is not a valid image.",
                details={"storage_path": storage_path, "reason": str(exc)},
            ) from exc

        if fmt not in _SUPPORTED_FORMATS:
            raise InvalidImageError(
                f"Uploaded image at {storage_path} has unsupported format {fmt!r}.",
                details={"storage_path": storage_path, "format": fmt},
            )

        checksum = hashlib.sha256(data).hexdigest()
        return ImageMetadata(
            width_px=width,
            height_px=height,
            bytes=len(data),
            checksum_sha256=checksum,
            mime_type=_SUPPORTED_FORMATS[fmt],
        )
    finally:
        local_path.unlink(missing_ok=True)
