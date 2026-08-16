"""Phase 20 Step 4 Checkpoint 4 — deterministic rough-composite and
seam-band logic. Pure PIL logic, no DB/Redis/Storage needed, mirroring how
tests/unit/test_recolor_service.py keeps compositing-style pixel logic in a
dedicated unit test rather than only exercising it indirectly through an
integration test.

Real seam-blend quality against actual jewelry photography is NOT covered
here and can't be from this environment — no real `GEMINI_API_KEY` or real
client pieces exist to test against, same category of gap as RECOLOR's
uncalibrated prong-bleed erosion. This only proves the deterministic
placement/seam-band *code* behaves as intended.
"""

import io

from PIL import Image

from app.services.mix_service import (
    _build_rough_composite,
    _build_seam_overlay,
    _composite_seam_result,
    _dilate,
    _erode,
    _feather,
    _seam_band_mask,
)


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _box_mask(size: tuple[int, int], box: tuple[int, int, int, int]) -> Image.Image:
    mask = Image.new("L", size, color=0)
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            mask.putpixel((x, y), 255)
    return mask


def test_dilate_grows_the_white_region() -> None:
    mask = _box_mask((40, 40), (15, 15, 25, 25))
    original_white = sum(1 for p in mask.getdata() if p == 255)

    dilated = _dilate(mask, px=2)
    dilated_white = sum(1 for p in dilated.getdata() if p == 255)

    assert dilated_white > original_white


def test_dilate_zero_px_is_a_noop() -> None:
    mask = _box_mask((10, 10), (4, 4, 6, 6))
    dilated = _dilate(mask, px=0)
    assert list(dilated.getdata()) == list(mask.getdata())


def test_seam_band_mask_is_a_ring_around_the_boundary() -> None:
    mask = _box_mask((40, 40), (15, 15, 25, 25))
    band = _seam_band_mask(mask, band_px=3)

    # Well inside the region (far from the boundary): not part of the seam.
    assert band.getpixel((20, 20)) == 0
    # Well outside the region: not part of the seam.
    assert band.getpixel((1, 1)) == 0
    # Right at the boundary: part of the seam.
    assert band.getpixel((15, 20)) == 255


def test_build_rough_composite_places_cropped_region_b_at_mask_a_bbox() -> None:
    """The single most important pure-logic test of this checkpoint (mirrors
    RECOLOR's own _build_overlay test) — proves the deterministic placement
    step works correctly *before* any provider call is even involved.
    """
    source_a = Image.new("RGB", (40, 40), color=(10, 10, 200))
    mask_a = _box_mask((40, 40), (10, 10, 20, 20))  # 10x10 region to receive the graft
    source_b = Image.new("RGB", (40, 40), color=(200, 10, 10))
    mask_b = _box_mask((40, 40), (5, 5, 15, 15))  # 10x10 region to cut from B

    composite_bytes = _build_rough_composite(
        _png_bytes(source_a), _png_bytes(mask_a), _png_bytes(source_b), _png_bytes(mask_b)
    )
    composite = Image.open(io.BytesIO(composite_bytes)).convert("RGB")

    # Inside mask A's bbox: region B's color (the graft).
    assert composite.getpixel((15, 15)) == (200, 10, 10)
    # Outside mask A's bbox entirely: source A's own, untouched pixel.
    assert composite.getpixel((35, 35)) == (10, 10, 200)


def test_build_rough_composite_leaves_pixels_outside_mask_a_bbox_untouched() -> None:
    source_a = Image.new("RGB", (40, 40), color=(10, 10, 200))
    mask_a = _box_mask((40, 40), (15, 15, 25, 25))
    source_b = Image.new("RGB", (40, 40), color=(200, 10, 10))
    mask_b = _box_mask((40, 40), (0, 0, 10, 10))

    composite_bytes = _build_rough_composite(
        _png_bytes(source_a), _png_bytes(mask_a), _png_bytes(source_b), _png_bytes(mask_b)
    )
    composite = Image.open(io.BytesIO(composite_bytes)).convert("RGB")

    for corner in [(1, 1), (38, 1), (1, 38), (38, 38)]:
        assert composite.getpixel(corner) == (10, 10, 200)


def test_build_rough_composite_handles_different_aspect_ratios_without_crashing() -> None:
    """Pins the documented, deliberately non-aspect-preserving scale-to-fit
    behavior — see phases/phase-20-mix.md Step 3's note. A tall region B
    stretched into a wide region A must not crash or silently crop.
    """
    source_a = Image.new("RGB", (40, 40), color=(10, 10, 200))
    mask_a = _box_mask((40, 40), (5, 15, 35, 25))  # wide: 30x10
    source_b = Image.new("RGB", (40, 40), color=(200, 10, 10))
    mask_b = _box_mask((40, 40), (15, 5, 25, 35))  # tall: 10x30

    composite_bytes = _build_rough_composite(
        _png_bytes(source_a), _png_bytes(mask_a), _png_bytes(source_b), _png_bytes(mask_b)
    )
    composite = Image.open(io.BytesIO(composite_bytes)).convert("RGB")
    assert composite.size == (40, 40)
    assert composite.getpixel((20, 20)) == (200, 10, 10)


def test_build_seam_overlay_paints_magenta_only_inside_the_seam_band() -> None:
    rough = Image.new("RGB", (40, 40), color=(10, 10, 200))
    mask_a = _box_mask((40, 40), (15, 15, 25, 25))
    band = _seam_band_mask(mask_a, band_px=3)

    overlay_bytes = _build_seam_overlay(_png_bytes(rough), band)
    overlay = Image.open(io.BytesIO(overlay_bytes)).convert("RGB")

    # Well inside the graft's interior (not part of the seam ring): untouched.
    assert overlay.getpixel((20, 20)) == (10, 10, 200)
    # Well outside the graft entirely: untouched.
    assert overlay.getpixel((1, 1)) == (10, 10, 200)
    # Right at the boundary (part of the seam ring): magenta.
    assert overlay.getpixel((15, 20)) == (255, 0, 255)


def test_composite_seam_result_is_bounded_to_the_feathered_seam_band() -> None:
    """Mirrors RECOLOR's own off-mask pixel identity test — everywhere the
    feathered seam band is 0, the result must be rough_composite's own
    pixel, exactly, regardless of what the provider changed."""
    rough = Image.new("RGB", (60, 60), color=(10, 10, 200))
    # A large box so its center sits well beyond the seam band's own width
    # (band_px=3) plus the compositing feather (settings.MASK_FEATHER_PX=3)
    # — a combined influence radius of ~6px from the boundary.
    mask_a = _box_mask((60, 60), (10, 10, 50, 50))
    band = _seam_band_mask(mask_a, band_px=3)
    # Simulate "the model changed everything, not just the seam."
    provider_output = Image.new("RGB", (60, 60), color=(0, 255, 0))

    result_bytes = _composite_seam_result(_png_bytes(rough), _png_bytes(provider_output), band)
    result = Image.open(io.BytesIO(result_bytes)).convert("RGB")

    for corner in [(1, 1), (58, 1), (1, 58), (58, 58)]:
        assert result.getpixel(corner) == (10, 10, 200)
    # Well inside the graft's interior (outside the seam band): also untouched.
    assert result.getpixel((30, 30)) == (10, 10, 200)


def test_erode_and_feather_are_available_for_reuse_shape() -> None:
    """mix_service.py reimplements erode/feather as its own module-level
    functions rather than importing recolor_service.py's — see this
    module's own docstring on why. Sanity-check the shape is unchanged."""
    mask = _box_mask((10, 10), (2, 2, 8, 8))
    assert _erode(mask, px=0) is mask or list(_erode(mask, px=0).getdata()) == list(mask.getdata())
    feathered = _feather(mask, px=2)
    assert any(0 < v < 255 for v in feathered.getdata())
