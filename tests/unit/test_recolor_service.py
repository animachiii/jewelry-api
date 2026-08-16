"""Phase 19 Step 4 Checkpoint 4 — erosion behaviour on a deliberately sloppy
mask. Pure PIL logic, no DB/Redis/Storage needed, mirroring how this
project keeps compositing-style pixel logic in a dedicated unit test rather
than only exercising it indirectly through an integration test.

Real prong-bleed calibration against actual jewelry photography is NOT
covered here and can't be from this environment — no real `GEMINI_API_KEY`
or real client mask exists to test against, same category of gap as the
uncalibrated QA thresholds. This only proves the erosion *code* shrinks the
marked region as intended.
"""

import io

from PIL import Image

from app.services.recolor_service import _build_overlay, _erode, _feather


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_erode_shrinks_the_white_region() -> None:
    mask = Image.new("L", (40, 40), color=0)
    for y in range(10, 30):
        for x in range(10, 30):
            mask.putpixel((x, y), 255)
    original_white = sum(1 for p in mask.getdata() if p == 255)

    eroded = _erode(mask, px=2)
    eroded_white = sum(1 for p in eroded.getdata() if p == 255)

    assert eroded_white < original_white


def test_erode_zero_px_is_a_noop() -> None:
    mask = Image.new("L", (10, 10), color=0)
    mask.putpixel((5, 5), 255)
    eroded = _erode(mask, px=0)
    assert list(eroded.getdata()) == list(mask.getdata())


def test_feather_softens_the_edge_into_intermediate_values() -> None:
    mask = Image.new("L", (40, 40), color=0)
    for y in range(10, 30):
        for x in range(10, 30):
            mask.putpixel((x, y), 255)

    feathered = _feather(mask, px=3)
    values = set(feathered.getdata())
    # A blur must introduce values strictly between 0 and 255 near the edge
    # — that's the whole point of feathering the compositing alpha.
    assert any(0 < v < 255 for v in values)


def test_build_overlay_paints_magenta_only_inside_the_eroded_mask() -> None:
    source = Image.new("RGB", (40, 40), color=(10, 10, 200))
    mask = Image.new("L", (40, 40), color=0)
    for y in range(10, 30):
        for x in range(10, 30):
            mask.putpixel((x, y), 255)

    overlay_bytes = _build_overlay(_png_bytes(source), _png_bytes(mask))
    overlay = Image.open(io.BytesIO(overlay_bytes)).convert("RGB")

    # Well outside the mask box: still the original source color.
    assert overlay.getpixel((1, 1)) == (10, 10, 200)
    # Well inside the mask box, away from its (eroded) edge: magenta.
    assert overlay.getpixel((20, 20)) == (255, 0, 255)


def test_sloppy_mask_erosion_pulls_the_marked_region_off_a_simulated_prong() -> None:
    """A deliberately sloppy mask overlapping a simulated 'metal' region:
    erosion (MASK_ERODE_PX) must shrink the region actually painted magenta
    in the overlay sent to the provider, relative to the raw mask — proving
    the erosion step has a real effect on what Gemini is told to edit, not
    just that _erode() shrinks white pixels in isolation (the test above).
    """
    source = Image.new("RGB", (40, 40), color=(10, 10, 200))
    # Sloppy mask: covers the full 15..25 gemstone region AND bleeds 3px
    # onto the surrounding "metal" (everything outside that box).
    mask = Image.new("L", (40, 40), color=0)
    for y in range(12, 28):
        for x in range(12, 28):
            mask.putpixel((x, y), 255)

    overlay_bytes = _build_overlay(_png_bytes(source), _png_bytes(mask))
    overlay = Image.open(io.BytesIO(overlay_bytes)).convert("RGB")

    # The bled-onto-metal edge pixel of the raw mask must NOT be magenta in
    # the eroded overlay — erosion pulled the edit region back off it.
    assert overlay.getpixel((12, 20)) != (255, 0, 255)
    # The gemstone's own center must still be magenta.
    assert overlay.getpixel((20, 20)) == (255, 0, 255)
