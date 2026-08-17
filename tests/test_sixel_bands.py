"""Band-level sixel preparation and rectangle encoding.

A sixel band is six pixel rows spanning the full width. Preparing a picture once
and cutting rectangles out of it is what makes scrolling a Markdown document
with embedded images affordable: a partially visible image's crop changes on
every scroll step, so under the naive flow every step re-ran decode, scale,
quantize and encode — measured at over a second per step on a README screenshot.

The two axes are deliberately not symmetric, and the tests say so: vertical
movement selects a different SET of bands and reuses each one, horizontal
movement changes every band and only reuses the preparation.
"""

import time

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from puikit.backends import _terminal_graphics as tg  # noqa: E402


@pytest.fixture(scope="module")
def picture():
    """Something with real colour variety — a flat fill would compress to
    nothing and hide the costs these tests are about."""
    img = Image.new("RGB", (120, 60))
    px = img.load()
    for y in range(60):
        for x in range(120):
            px[x, y] = ((x * 5 + y) % 256, (y * 7) % 256, (x * 3 + y * 2) % 256)
    return img


def test_whole_image_matches_the_direct_encoder(picture):
    # The band path must be byte-for-byte what the one-shot encoder produced,
    # or every terminal that renders it is now looking at different bytes.
    source = tg.prepare_sixel(picture)
    whole = source.encode_rect(0, 0, source.width, source.height)
    assert whole == tg._sixel(picture)


def test_vertical_crop_selects_bands(picture):
    source = tg.prepare_sixel(picture)
    full = source.encode_rect(0, 0, 120, 60)
    scrolled = source.encode_rect(0, 12, 120, 60)
    assert scrolled != full
    # Two bands fewer, so the declared height drops by 12 rows.
    assert '"1;1;120;48' in scrolled
    assert '"1;1;120;60' in full


def test_the_top_snaps_down_to_a_band_boundary(picture):
    # A band is the smallest unit sixel can express, so a crop starting mid-band
    # begins at the band that contains it — up to five pixels early, under a
    # fifth of a cell.
    source = tg.prepare_sixel(picture)
    a = source.encode_rect(0, 12, 120, 54)
    b = source.encode_rect(0, 14, 120, 56)
    assert a.split("q", 1)[1][:14] == b.split("q", 1)[1][:14]  # same first band


@pytest.mark.parametrize("y0,y1", [(0, 60), (4, 60), (10, 48), (7, 37), (1, 7)])
def test_never_emits_more_rows_than_asked_for(picture, y0, y1):
    # Rounding the BOTTOM out too would overshoot the cell box the caller
    # reserved, and those pixels land in the row below it — a row the frame diff
    # has no reason to re-send, since its text did not change. Scrolling an image
    # upward then leaves a stripe behind on every step.
    import re

    source = tg.prepare_sixel(picture)
    out = source.encode_rect(0, y0, 120, y1)
    declared = int(re.search(r'"1;1;\d+;(\d+)', out).group(1))
    assert declared <= y1 - y0, (declared, y1 - y0)


def test_repeated_vertical_crops_reuse_the_encoded_bands(picture):
    # The point of the whole exercise: after the first pass over a band, moving
    # up and down costs essentially nothing.
    source = tg.prepare_sixel(picture)
    start = time.perf_counter()
    source.encode_rect(0, 0, 120, 60)
    first = time.perf_counter() - start
    start = time.perf_counter()
    for top in range(6, 30, 6):
        source.encode_rect(0, top, 120, 60)
    repeats = (time.perf_counter() - start) / 4
    assert repeats < first / 2, (first, repeats)


def test_horizontal_crop_narrows_every_band(picture):
    # Not symmetric with the vertical case: each band is a full-width strip, so
    # a horizontal crop changes all of them and they must be re-encoded.
    source = tg.prepare_sixel(picture)
    cropped = source.encode_rect(20, 0, 100, 60)
    assert '"1;1;80;60' in cropped
    assert cropped != source.encode_rect(0, 0, 120, 60)


def test_rect_is_clamped_to_the_picture(picture):
    source = tg.prepare_sixel(picture)
    assert source.encode_rect(-50, -50, 500, 500) == source.encode_rect(0, 0, 120, 60)


def test_empty_rect_still_terminates_the_sequence(picture):
    source = tg.prepare_sixel(picture)
    out = source.encode_rect(0, 0, 0, 0)
    assert out.startswith("\x1bP")
    assert out.endswith("\x1b\\")


def test_preparation_is_independent_of_the_rectangle(picture):
    # Same prepared source, two different rectangles, no cross-contamination:
    # asking for one must not change what the other returns.
    source = tg.prepare_sixel(picture)
    a1 = source.encode_rect(0, 0, 60, 30)
    _b = source.encode_rect(30, 12, 120, 60)
    a2 = source.encode_rect(0, 0, 60, 30)
    assert a1 == a2


# --- source identity -------------------------------------------------------


def test_a_rewritten_file_is_a_different_source(tmp_path):
    # A path names a LOCATION, and the same location holds different pixels
    # after a rebuild, a thumbnail refresh, or a file replaced mid-copy. Keying a
    # cache on the path alone serves the old picture until it happens to be
    # evicted — which for a file manager is exactly the wrong behaviour.
    import os

    path = tmp_path / "shot.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    before = tg.source_key(str(path))
    Image.new("RGB", (8, 8), (200, 30, 40)).save(path)
    # Stamp a distinct mtime AFTER the write. Two saves a few microseconds apart
    # can land on the same filesystem timestamp, which is the known limit of
    # mtime-based invalidation rather than something this can fix.
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))
    assert tg.source_key(str(path)) != before


def test_an_unchanged_file_keeps_its_identity(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    assert tg.source_key(str(path)) == tg.source_key(str(path))


def test_a_missing_file_does_not_raise(tmp_path):
    # The picture cannot be loaded either, so nothing is cached against it.
    key = tg.source_key(str(tmp_path / "gone.png"))
    assert key[0] == "path"


def test_a_source_may_name_its_own_identity():
    # The shape that lets a raster source — pixel data handed straight to the
    # backend, as a photo editor would — slot in without any cache changing.
    # Hashing the content per frame would cost more than the encode it avoids,
    # so such a source carries a revision it bumps when written to.
    class Raster:
        def __init__(self, revision):
            self.cache_key = ("buffer-7", revision)

    first = tg.source_key(Raster(1))
    assert first[0] == "raster"
    assert first != tg.source_key(Raster(2))     # an edit invalidates
    assert first == tg.source_key(Raster(1))     # an unchanged buffer does not
