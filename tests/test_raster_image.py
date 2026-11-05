"""Decoded pixels as an image source, and the caches that have to notice.

``draw_image`` used to take a path and only a path, which meant an application
holding pixels it decoded itself had to write a PNG back out and pass its name —
an encode and a decode to move data the backend was about to be handed. These
cover the other half of the contract: the :class:`~puikit.image.RasterImage`
type, the identity every cache keys on, the byte budget that identity made
necessary, and each backend's answer to "which formats do you read yourself?".

The GUI backends are covered where they can be: the macOS ones actually build an
``NSImage`` and read its pixels back, and are skipped elsewhere. The Windows path
has no equivalent here — it needs a live Direct2D render target — and is checked
by hand on a Windows machine.
"""

import io

import pytest

from puikit.backend import Backend
from puikit.backends import _terminal_graphics
from puikit.backends._image_cache import DEFAULT_BUDGET, MISS, ImageCache
from puikit.backends.memory_backend import MemoryBackend
from puikit.image import RasterImage, is_raster, source_key


def _solid(w, h, color=(10, 200, 30, 255)):
    return RasterImage(w, h, bytes(color) * (w * h))


# --------------------------------------------------------------------------- #
# The type
# --------------------------------------------------------------------------- #

def test_a_raster_reports_its_own_size_and_weight():
    raster = _solid(4, 3)
    assert raster.size == (4, 3)
    assert raster.width, raster.height == (4, 3)
    assert raster.nbytes == 4 * 3 * 4


def test_a_buffer_that_does_not_match_the_size_is_refused():
    # Silently accepting a short buffer would hand the backend a stride it reads
    # past the end of — a crash in native code, a long way from the mistake.
    with pytest.raises(ValueError):
        RasterImage(4, 3, b"\x00" * 16)
    with pytest.raises(ValueError):
        RasterImage(0, 3, b"")


def test_the_revision_moves_only_when_the_pixels_are_declared_changed():
    raster = _solid(2, 2)
    first = raster.cache_key
    assert raster.cache_key == first  # merely reading changes nothing

    raster.touch()
    assert raster.cache_key != first
    assert raster.cache_key[0] == first[0]  # same raster, later picture


def test_update_replaces_the_size_with_the_pixels():
    raster = _solid(2, 2)
    raster.update(1, 4, bytes((1, 2, 3, 4)) * 4)
    assert raster.size == (1, 4)
    assert raster.cache_key[1] == 1


def test_two_rasters_never_share_an_identity():
    assert _solid(1, 1).cache_key[0] != _solid(1, 1).cache_key[0]


def test_pillow_round_trips_through_a_raster():
    Image = pytest.importorskip("PIL.Image")
    source = Image.new("RGB", (3, 2), (10, 20, 30))

    raster = RasterImage.from_pillow(source)
    assert raster.size == (3, 2)

    back = raster.to_pillow()
    assert back.mode == "RGBA"
    assert back.getpixel((0, 0)) == (10, 20, 30, 255)


def test_is_raster_asks_about_shape_not_class():
    class OwnBuffer:
        cache_key = (1, 0)
        size = (2, 2)

    assert is_raster(OwnBuffer())
    assert is_raster(_solid(1, 1))
    assert not is_raster("/tmp/picture.png")


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #

def test_source_key_tells_a_raster_from_a_path():
    raster = _solid(1, 1)
    assert source_key(raster) == ("raster", *raster.cache_key)
    assert source_key("/no/such/file.png")[0] == "path"


def test_a_path_is_identified_by_its_contents_not_its_name(tmp_path):
    # The whole reason a cache cannot key on the path alone: the same name holds
    # different pixels after a rebuild.
    picture = tmp_path / "pic.bin"
    picture.write_bytes(b"first")
    before = source_key(str(picture))
    picture.write_bytes(b"second-and-longer")
    assert source_key(str(picture)) != before


# --------------------------------------------------------------------------- #
# The budgeted cache
# --------------------------------------------------------------------------- #

def test_the_least_recently_used_entry_is_the_one_evicted():
    gone = []
    cache = ImageCache(budget=100, on_evict=gone.append)
    cache.put("a", "A", 50)
    cache.put("b", "B", 50)
    cache.get("a")                     # 'a' is now the fresher of the two
    cache.put("c", "C", 50)

    assert gone == ["B"]
    assert cache.get("b") is MISS
    assert cache.get("a") == "A"


def test_an_image_larger_than_the_whole_budget_is_still_kept():
    # Refusing it would mean the pictures that most need a cache never get one.
    cache = ImageCache(budget=100)
    cache.put("huge", "H", 10_000)
    assert cache.get("huge") == "H"
    assert len(cache) == 1


def test_a_failed_decode_is_remembered_as_a_result():
    # Otherwise a missing file is retried on every single frame.
    cache = ImageCache()
    cache.put("broken", None, 0)
    assert cache.get("broken") is None
    assert cache.get("never-seen") is MISS


def test_clearing_releases_every_held_value():
    released = []
    cache = ImageCache(on_evict=released.append)
    cache.put("a", "A", 1)
    cache.put("b", None, 0)
    cache.clear()

    assert released == ["A"]        # None owns nothing, so it is not released
    assert len(cache) == 0 and cache.nbytes == 0


def test_the_default_budget_is_large_enough_for_a_real_photograph():
    # A 24-megapixel frame is ~96MB of RGBA; a budget under that would evict it
    # before it was drawn twice.
    assert DEFAULT_BUDGET >= 6000 * 4000 * 4


# --------------------------------------------------------------------------- #
# The backend contract
# --------------------------------------------------------------------------- #

def test_the_base_backend_measures_a_raster_without_touching_a_file():
    assert Backend.image_size(object(), _solid(5, 7)) == (5, 7)


def test_a_backend_claims_no_formats_until_it_says_otherwise():
    # Empty means "I have not answered", never "nothing works" — a caller must
    # not read it as a reason to give up.
    assert Backend.image_formats(object()) == frozenset()


def test_a_backend_that_cannot_draw_images_claims_no_formats():
    from puikit.panel import Panel

    backend = MemoryBackend(20, 10)
    backend.supported_image_formats = frozenset({".heic"})
    assert not backend.capabilities.supports("images")
    assert Panel(backend).image_formats() == frozenset()


def test_a_backend_passes_a_raster_through_untouched():
    backend = MemoryBackend(20, 10)
    raster = _solid(2, 2)
    backend.draw_image(1, 2, raster)
    assert backend.image_calls == [(1, 2, raster, {})]


# --------------------------------------------------------------------------- #
# The terminal path
# --------------------------------------------------------------------------- #

def test_the_terminal_renders_a_raster_and_a_file_identically(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    picture = Image.new("RGB", (8, 6), (200, 40, 40))
    path = tmp_path / "pic.png"
    picture.save(path)

    from_file = _terminal_graphics.render(str(path), 8, 6)
    from_raster = _terminal_graphics.render(RasterImage.from_pillow(picture), 8, 6)

    assert from_file is not None and from_raster is not None
    assert from_file[0].size == from_raster[0].size
    assert from_raster[0].getpixel((0, 0))[:3] == (200, 40, 40)


def test_the_terminal_reads_whatever_pillow_reads():
    pytest.importorskip("PIL.Image")
    extensions = _terminal_graphics.extensions()
    assert {".png", ".jpg", ".gif"} <= extensions
    assert all(e.startswith(".") and e == e.lower() for e in extensions)


def test_a_source_the_terminal_cannot_open_renders_to_nothing():
    pytest.importorskip("PIL.Image")
    assert _terminal_graphics.render("/no/such/picture.png", 4, 4) is None


# --------------------------------------------------------------------------- #
# The web backend's wire format
# --------------------------------------------------------------------------- #

def test_a_repainted_raster_gets_a_new_asset_id():
    from puikit.backends.web_backend import _asset_id

    raster = _solid(2, 2)
    before = _asset_id(raster)
    raster.touch()
    assert _asset_id(raster) != before


def test_a_raster_reaches_the_browser_as_a_png():
    pytest.importorskip("PIL.Image")
    from puikit.backends.web_backend import _png_bytes

    data = _png_bytes(_solid(3, 3))
    assert data is not None and data[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_browser_reads_the_formats_no_application_should_have_to_decode():
    from puikit.backends.web_backend import _BROWSER_IMAGE_FORMATS

    assert {".png", ".jpg", ".gif", ".webp"} <= _BROWSER_IMAGE_FORMATS


# --------------------------------------------------------------------------- #
# macOS, where a real decoder can be asked
# --------------------------------------------------------------------------- #

macos = pytest.mark.skipif(
    not __import__("sys").platform.startswith("darwin"), reason="macOS only")


@macos
def test_appkit_reports_extensions_not_classic_type_codes():
    pytest.importorskip("AppKit")
    from puikit._platform_image import extensions as platform_extensions

    extensions = platform_extensions()
    assert {".png", ".jpg", ".gif", ".tiff"} <= extensions
    # imageFileTypes() mixes in four-character OSType codes ("'jpeg'", "'bmp '");
    # a caller holding a filename can do nothing with those.
    assert all(e[1:].isalnum() for e in extensions)


@macos
def test_appkit_reads_more_than_the_header_parse_knows():
    # The point of asking the system rather than listing formats: HEIC is here
    # on any current macOS, and is exactly what an application would otherwise
    # have had to find a decoder for.
    pytest.importorskip("AppKit")
    from puikit._platform_image import extensions as platform_extensions

    assert ".heic" in platform_extensions()


@macos
def test_imageio_reports_the_stored_pixel_size(tmp_path):
    pytest.importorskip("AppKit")
    Image = pytest.importorskip("PIL.Image")
    from puikit.backends.macos_backend import _imageio_pixel_size

    path = tmp_path / "pic.png"
    Image.new("RGB", (37, 11), (0, 0, 0)).save(path)
    assert _imageio_pixel_size(str(path)) == (37, 11)
    assert _imageio_pixel_size(str(tmp_path / "absent.png")) is None


@macos
def test_a_raster_becomes_an_nsimage_with_the_right_pixels():
    pytest.importorskip("AppKit")
    from puikit.backends.macos_backend import _image_from_raster

    raster = RasterImage(2, 1, bytes((255, 0, 0, 255)) + bytes((0, 0, 255, 128)))
    image = _image_from_raster(raster)
    assert image is not None
    # Sized in pixels, not in the points an NSImage would derive from the rep's
    # DPI — every fit and crop above this line measures in whatever size says.
    assert (image.size().width, image.size().height) == (2, 1)

    rep = image.representations()[0]
    left = rep.colorAtX_y_(0, 0)
    right = rep.colorAtX_y_(1, 0)
    assert round(left.redComponent()) == 1 and round(left.blueComponent()) == 0
    # Straight alpha: the blue channel survives at half opacity rather than
    # having been scaled into the color the way premultiplication would.
    assert round(right.blueComponent()) == 1
    assert 0.4 < right.alphaComponent() < 0.6


class TestARastersEntriesGoWithIt:

    def _raster(self):
        return RasterImage(1, 1, b"\0\0\0\0")

    def test_they_stay_while_it_lives(self):
        cache = ImageCache()
        raster = self._raster()
        key = source_key(raster)
        cache.put(key, "native", 4, owner=raster)
        cache.drain()
        assert cache.get(key) == "native"

    def test_they_go_on_the_drain_after_it_dies(self):
        gone = []
        cache = ImageCache(on_evict=gone.append)
        raster = self._raster()
        cache.put(source_key(raster), "native", 4, owner=raster)
        del raster
        # Not yet: a raster can die on any thread, and the entries are the UI
        # thread's to touch.
        assert gone == [] and cache.nbytes == 4
        cache.drain()
        assert gone == ["native"] and cache.nbytes == 0 and len(cache) == 0

    def test_a_death_asks_for_a_drain_and_touches_nothing_itself(self):
        woken = []
        cache = ImageCache(wake=lambda: woken.append(len(cache)))
        raster = self._raster()
        cache.put(source_key(raster), "native", 4, owner=raster)
        del raster
        assert woken == [1]

    def test_a_failing_wake_is_left_to_the_next_put(self):
        def wake():
            raise RuntimeError("no window yet")
        cache = ImageCache(wake=wake)
        raster = self._raster()
        cache.put(source_key(raster), "native", 4, owner=raster)
        del raster
        cache.put("other", "other", 1)
        assert len(cache) == 1 and cache.get("other") == "other"

    def test_every_revision_goes(self):
        cache = ImageCache()
        raster = self._raster()
        cache.put(source_key(raster), "first", 4, owner=raster)
        raster.touch()
        cache.put(source_key(raster), "second", 4, owner=raster)
        del raster
        cache.drain()
        assert len(cache) == 0

    def test_a_path_has_no_owner_and_stays(self):
        cache = ImageCache()
        cache.put(("path", "a.png", 1, 1), "native", 4)
        cache.drain()
        assert len(cache) == 1

    def test_one_evicted_by_the_budget_is_not_dropped_twice(self):
        gone = []
        cache = ImageCache(budget=4, on_evict=gone.append)
        raster = self._raster()
        cache.put(source_key(raster), "native", 4, owner=raster)
        cache.put("other", "other", 4)
        assert gone == ["native"]
        del raster
        cache.drain()
        assert gone == ["native"] and len(cache) == 1

    def test_an_own_buffer_that_cannot_be_weakly_referenced_goes_by_the_budget(self):
        class Buffer:
            __slots__ = ()
            cache_key = (1, 0)
        cache = ImageCache()
        cache.put(("raster", 1, 0), "native", 4, owner=Buffer())
        cache.drain()
        assert len(cache) == 1
