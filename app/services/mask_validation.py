"""Mask contract validation for RECOLOR (Phase 19). Sibling to
image_validation.py, not a modification of it — a mask is validated against
entirely different rules (single-channel, binary-valued, dimension-locked
to a specific other image) that would only complicate
image_validation.inspect_and_validate's existing contract for ordinary
photos. Reuses storage_service.download_to_temp and Pillow, same as
image_validation.py. See phases/phase-19-recolor.md Step 3 and
docs/business-rules.md §15's Mask Contract table.

Every violation raises InvalidMaskError (422 VALIDATION_ERROR, same code
image_validation.InvalidImageError already uses — docs/conventions.md's
"name the specific violation" rule is met via `details`, not a dedicated
per-rule error code) and names the specific rule that failed, never a
generic "invalid mask" message.
"""

import hashlib
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from app.config import settings
from app.core.errors import AppError, ErrorCode
from app.services import storage_service

_VALID_MASK_VALUES = frozenset({0, 255})


class InvalidMaskError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422


@dataclass(frozen=True)
class MaskMetadata:
    width_px: int
    height_px: int
    bytes: int
    checksum_sha256: str
    coverage_pct: float


def validate_mask(
    bucket: str, storage_path: str, source_width_px: int, source_height_px: int
) -> MaskMetadata:
    """Downloads the object at `storage_path` and validates it against the
    mask contract, dimension-checked against the RECOLOR job's own source
    image. Raises `InvalidMaskError` naming the specific violation. Never
    raises for a missing object — callers are expected to have already
    confirmed existence via `storage_service.exists`, same posture
    `image_validation.inspect_and_validate` documents.
    """
    local_path = storage_service.download_to_temp(bucket, storage_path)
    try:
        data = local_path.read_bytes()
        if len(data) == 0:
            raise InvalidMaskError(
                f"Uploaded mask at {storage_path} is empty.",
                details={"storage_path": storage_path},
            )

        try:
            with Image.open(local_path) as img:
                img.verify()
            with Image.open(local_path) as img2:
                fmt = img2.format
                mode = img2.mode
                width, height = img2.size
                # Getting the histogram now, inside the `with` block, avoids
                # a second full decode — PIL.Image.histogram() answers
                # "which of the 256 possible byte values appear at least
                # once" without materializing a full pixel set, so this
                # scales to a large mask without the memory cost
                # `set(img.getdata())` would carry.
                histogram = img2.histogram() if mode == "L" else None
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise InvalidMaskError(
                f"Uploaded mask at {storage_path} is not a valid image.",
                details={"storage_path": storage_path, "reason": str(exc)},
            ) from exc

        if fmt != "PNG":
            raise InvalidMaskError(
                f"Mask must be PNG, received {fmt!r}.",
                details={"storage_path": storage_path, "format": fmt},
            )

        if mode != "L":
            raise InvalidMaskError(
                "Mask must be single-channel 8-bit grayscale, "
                f"received mode {mode!r}. Do not send a color or alpha-channel image.",
                details={"storage_path": storage_path, "mode": mode},
            )

        if (width, height) != (source_width_px, source_height_px):
            raise InvalidMaskError(
                f"Mask dimensions {width}x{height} do not match "
                f"source {source_width_px}x{source_height_px}.",
                details={
                    "storage_path": storage_path,
                    "mask_width_px": width,
                    "mask_height_px": height,
                    "source_width_px": source_width_px,
                    "source_height_px": source_height_px,
                },
            )

        assert histogram is not None  # mode == "L" confirmed above
        populated_values = {value for value in range(256) if histogram[value] > 0}
        intermediate_values = populated_values - _VALID_MASK_VALUES
        if intermediate_values:
            raise InvalidMaskError(
                f"Mask must be binary (0 and 255 only); found "
                f"{len(intermediate_values)} intermediate value(s).",
                details={
                    "storage_path": storage_path,
                    "intermediate_value_count": len(intermediate_values),
                },
            )

        white_pixels = histogram[255]
        total_pixels = width * height
        coverage_pct = (white_pixels / total_pixels) * 100 if total_pixels else 0.0

        if coverage_pct < settings.MASK_MIN_COVERAGE_PCT:
            raise InvalidMaskError(
                f"Mask covers {coverage_pct:.2f}% of the image, "
                f"minimum is {settings.MASK_MIN_COVERAGE_PCT}%.",
                details={"storage_path": storage_path, "coverage_pct": coverage_pct},
            )
        if coverage_pct > settings.MASK_MAX_COVERAGE_PCT:
            raise InvalidMaskError(
                f"Mask covers {coverage_pct:.2f}% of the image, "
                f"maximum is {settings.MASK_MAX_COVERAGE_PCT}%.",
                details={"storage_path": storage_path, "coverage_pct": coverage_pct},
            )

        checksum = hashlib.sha256(data).hexdigest()
        return MaskMetadata(
            width_px=width,
            height_px=height,
            bytes=len(data),
            checksum_sha256=checksum,
            coverage_pct=coverage_pct,
        )
    finally:
        local_path.unlink(missing_ok=True)
