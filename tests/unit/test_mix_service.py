"""MIX's highlight-building logic. Pure PIL, no DB/Redis/Storage needed,
mirroring how tests/unit/test_recolor_service.py keeps compositing-style pixel
logic in a dedicated unit test rather than only exercising it indirectly
through an integration test.

**Rewritten 2026-08-31 alongside the generative MIX rewrite.** This file used
to cover `_build_rough_composite`, `_seam_band_mask`, `_build_seam_overlay` and
`_composite_seam_result` — the deterministic graft pipeline and its
byte-identical-outside-the-seam-band guarantee. None of those functions exist
any more (see `app/services/mix_service.py`'s module docstring for why), so
their tests are gone rather than adapted: they asserted a contract MIX no
longer makes. Deleted with them:

- `test_build_rough_composite_places_cropped_region_b_at_mask_a_bbox`
- `test_graft_excludes_unpainted_parts_of_mask_b_bbox`
- `test_graft_preserves_aspect_ratio_of_region_b`
- `test_composite_seam_result_is_bounded_to_the_feathered_seam_band`
  (and the integration-level `test_mix_output_is_byte_identical_to_
  rough_composite_outside_seam_band`)

What replaces them is narrower on purpose. The old tests could prove real
things about the output because the output was deterministic; MIX's output is
now a model response, so what is left to pin mechanically is that the two
reference images we *send* are built correctly — the region is marked, the rest
of the photo is untouched, and the marked jewelry stays visible under the mark.

Whether the resulting design is any good is not testable here and never will
be from CI (docs/ai-integration.md's "never call the live Gemini API in
tests"). That judgement needs a real call against real client pieces.
"""

import io

import pytest
from PIL import Image

from app.config import settings
from app.services.mix_service import (
    _HIGHLIGHT_OUTLINE_PX,
    _HIGHLIGHT_PRIMARY,
    _HIGHLIGHT_SECONDARY,
    _HIGHLIGHT_TINT_ALPHA,
    _build_highlight,
    _dilate,
    _erode,
    _load_downscaled,
    _outline_mask,
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


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


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


def test_erode_shrinks_the_white_region() -> None:
    mask = _box_mask((40, 40), (15, 15, 25, 25))
    original_white = sum(1 for p in mask.getdata() if p == 255)

    eroded = _erode(mask, px=2)
    eroded_white = sum(1 for p in eroded.getdata() if p == 255)

    assert eroded_white < original_white


def test_outline_mask_is_a_ring_around_the_boundary() -> None:
    mask = _box_mask((40, 40), (10, 10, 30, 30))
    ring = _outline_mask(mask, px=3)

    # On the boundary: white. Deep inside and far outside: black.
    assert ring.getpixel((10, 20)) == 255
    assert ring.getpixel((20, 20)) == 0
    assert ring.getpixel((2, 2)) == 0


def test_outline_mask_zero_px_is_empty() -> None:
    mask = _box_mask((20, 20), (5, 5, 15, 15))
    assert set(_outline_mask(mask, px=0).getdata()) == {0}


def test_highlight_leaves_pixels_outside_the_mask_byte_identical() -> None:
    """The surrounding piece is context the model needs — that this element
    hangs from that chain, at that scale, in that metal. Nothing outside the
    client's painted region may be altered.
    """
    source = Image.new("RGB", (60, 60), color=(10, 120, 40))
    mask = _box_mask((60, 60), (20, 20, 40, 40))

    out = _open(_build_highlight(_png_bytes(source), _png_bytes(mask), _HIGHLIGHT_PRIMARY))

    # Well clear of the region and of the outline ring that straddles it.
    for point in ((2, 2), (58, 2), (2, 58), (58, 58), (10, 30)):
        assert out.getpixel(point) == (10, 120, 40), f"pixel {point} was modified"


def test_highlight_tints_the_masked_region_toward_the_marker_colour() -> None:
    source = Image.new("RGB", (60, 60), color=(0, 0, 0))
    mask = _box_mask((60, 60), (20, 20, 40, 40))

    out = _open(_build_highlight(_png_bytes(source), _png_bytes(mask), _HIGHLIGHT_PRIMARY))

    # Interior, away from the outline ring. Black blended 30% toward magenta.
    r, g, b = out.getpixel((30, 30))
    expected = round(255 * _HIGHLIGHT_TINT_ALPHA)
    assert abs(r - expected) <= 1
    assert g == 0
    assert abs(b - expected) <= 1


def test_highlight_keeps_the_marked_jewelry_visible() -> None:
    """The single most important property, and the one that separates this
    from RECOLOR's overlay: the marked region is what Gemini must *reproduce*,
    so it cannot be painted over opaquely the way RECOLOR paints a region it
    wants replaced. Two different source colours under the same mark must stay
    distinguishable in the output.
    """
    mask = _box_mask((60, 60), (20, 20, 40, 40))
    light = Image.new("RGB", (60, 60), color=(230, 200, 120))  # gold
    dark = Image.new("RGB", (60, 60), color=(20, 90, 60))  # emerald

    light_out = _open(_build_highlight(_png_bytes(light), _png_bytes(mask), _HIGHLIGHT_PRIMARY))
    dark_out = _open(_build_highlight(_png_bytes(dark), _png_bytes(mask), _HIGHLIGHT_PRIMARY))

    light_px = light_out.getpixel((30, 30))
    dark_px = dark_out.getpixel((30, 30))
    assert light_px != dark_px
    # Not merely different — still clearly separable, not crushed toward the
    # marker colour. Green is the channel magenta contributes nothing to.
    assert light_px[1] - dark_px[1] > 50


def test_highlight_draws_a_solid_outline_on_the_boundary() -> None:
    source = Image.new("RGB", (60, 60), color=(128, 128, 128))
    mask = _box_mask((60, 60), (20, 20, 40, 40))

    out = _open(_build_highlight(_png_bytes(source), _png_bytes(mask), _HIGHLIGHT_PRIMARY))

    # The ring straddles the boundary, so the boundary pixel itself is solid
    # marker colour rather than a tint of the source.
    assert out.getpixel((20, 30)) == _HIGHLIGHT_PRIMARY
    assert _HIGHLIGHT_OUTLINE_PX > 0


def test_the_two_marker_colours_are_distinct() -> None:
    """The prompt distinguishes the two pieces by colour alone (migration
    0020), so a change that made these equal would silently destroy the
    pairing while every other test still passed.
    """
    assert _HIGHLIGHT_PRIMARY != _HIGHLIGHT_SECONDARY


def test_highlight_uses_the_colour_it_is_given() -> None:
    source = Image.new("RGB", (60, 60), color=(0, 0, 0))
    mask = _box_mask((60, 60), (20, 20, 40, 40))

    out = _open(_build_highlight(_png_bytes(source), _png_bytes(mask), _HIGHLIGHT_SECONDARY))

    # Cyan is (0, 255, 255): no red contribution, unlike magenta's tint.
    r, g, b = out.getpixel((30, 30))
    assert r == 0
    assert g > 0
    assert b > 0


def test_highlight_output_is_capped_at_working_max_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-08-27 OOM fix still applies — a real client upload is 12.6 MP
    and the instance has ~160 MB of headroom. See app/config.py's
    WORKING_MAX_EDGE note.
    """
    monkeypatch.setattr(settings, "WORKING_MAX_EDGE", 64)
    source = Image.new("RGB", (300, 200), color=(10, 20, 30))
    mask = _box_mask((300, 200), (100, 80, 200, 150))

    out = _open(_build_highlight(_png_bytes(source), _png_bytes(mask), _HIGHLIGHT_PRIMARY))

    assert max(out.size) == 64
    assert out.size == (64, 43)


def test_highlight_leaves_a_small_image_at_native_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "WORKING_MAX_EDGE", 2048)
    source = Image.new("RGB", (60, 40), color=(10, 20, 30))
    mask = _box_mask((60, 40), (20, 10, 40, 30))

    out = _open(_build_highlight(_png_bytes(source), _png_bytes(mask), _HIGHLIGHT_PRIMARY))

    assert out.size == (60, 40)


def test_downscaled_mask_stays_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_outline_mask`'s dilate-minus-erode assumes a binary mask; a LANCZOS
    resize would introduce intermediate values and soften the contour into a
    gradient. `_load_downscaled` uses NEAREST for masks specifically to
    prevent that.
    """
    monkeypatch.setattr(settings, "WORKING_MAX_EDGE", 40)
    mask = _box_mask((160, 160), (40, 40, 120, 120))

    downscaled = _load_downscaled(_png_bytes(mask), "L")

    assert max(downscaled.size) == 40
    assert set(downscaled.getdata()) <= {0, 255}
