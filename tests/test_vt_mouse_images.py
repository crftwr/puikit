"""Mouse input and inline images on the VT backend.

Windows reports mouse STATE, not gestures: every record says which buttons are
down now, so press / release / drag come from comparing against the previous
record. That is the part worth testing — the translation (_win_mouse_records),
not the ctypes — so the ``mouse()`` records here are raw MOUSE_EVENTs run
through it exactly as the real ``_read_records`` does.

Images matter here for a different reason. The curses backend already
implements them; they never appear on Windows because PDCurses displays a
different screen buffer than the one the escape is written to (xefm#306). Owning
the output stream is the whole fix, so what these check is that the payload is
emitted, positioned, and erased at the right moments.
"""

import io

import pytest

from puikit.backends import _terminal_graphics
from puikit.backends.vt_backend import VTBackend, _StreamConsole, _win_mouse_records
from puikit.event import EventType


def _placement(x, y, cols, rows, source):
    """One entry of ``VTBackend._images``, built the way ``draw_image`` builds
    it: the placement, then the source's cache key, which is what makes a
    changed picture at an unchanged position compare unequal."""
    from puikit.image import source_key

    return (x, y, cols, rows, source, None, (x, y, cols, rows, None),
            source_key(source))


class FakeConsole(_StreamConsole):
    def __init__(self, width=40, height=10):
        super().__init__(stream=io.StringIO(), size=(width, height))
        self._fixed = (width, height)
        self.queue: list[list[dict]] = []
        self._mouse_buttons = 0

    def size(self):
        return self._fixed

    def read_input(self, timeout_ms):
        return self.queue.pop(0) if self.queue else []

    def push_mouse(self, *raws):
        """Queue raw MOUSE_EVENT records, diffed into gestures through the same
        translation (and the same running button state) as the real console."""
        out = []
        for raw in raws:
            gestures, self._mouse_buttons = _win_mouse_records(raw, self._mouse_buttons)
            out.extend(gestures)
        self.queue.append(out)


def mouse(x=0, y=0, buttons=0, flags=0, wheel=0, control=0):
    return {"x": x, "y": y, "buttons": buttons,
            "flags": flags, "wheel": wheel, "control": control}


@pytest.fixture
def backend():
    be = VTBackend(console=FakeConsole())
    be.open()
    yield be, be._console
    be.close()


@pytest.fixture
def png(tmp_path):
    """A real PNG on disk. Emission tests need one: a missing path makes
    render() return None, and every assertion about the payload then passes
    whether or not the code under test works."""
    pytest.importorskip("PIL")
    from PIL import Image

    path = tmp_path / "pic.png"
    Image.new("RGB", (8, 8), (200, 40, 40)).save(path)
    return str(path)


def drain(be, con):
    """Every event the backend produces from one queued batch."""
    got = []
    for _ in range(20):
        before = len(got)
        be.run_event_loop_iteration(got.append, 0)
        if len(got) == before and not be._pending:
            break
    return got


# --- mouse: buttons -------------------------------------------------------


def test_press_and_release_become_down_and_up(backend):
    be, con = backend
    con.push_mouse(mouse(x=5, y=3, buttons=0x0001), mouse(x=5, y=3, buttons=0))
    events = drain(be, con)
    assert [e.type for e in events] == [EventType.MOUSE_DOWN, EventType.MOUSE_UP]
    assert all(e.button == "left" for e in events)
    assert (events[0].x, events[0].y) == (5.0, 3.0)


def test_right_and_middle_buttons_are_named(backend):
    be, con = backend
    con.push_mouse(mouse(buttons=0x0002), mouse(buttons=0),
                   mouse(buttons=0x0004), mouse(buttons=0))
    buttons = [e.button for e in drain(be, con)]
    assert buttons == ["right", "right", "middle", "middle"]


def test_no_event_when_state_is_unchanged(backend):
    # Windows re-reports the same state freely; only transitions are gestures.
    be, con = backend
    con.push_mouse(mouse(buttons=0x0001), mouse(buttons=0x0001))
    assert [e.type for e in drain(be, con)] == [EventType.MOUSE_DOWN]


# --- mouse: motion --------------------------------------------------------


def test_motion_with_a_button_held_is_a_drag(backend):
    be, con = backend
    con.push_mouse(mouse(x=1, y=1, buttons=0x0001),
                   mouse(x=4, y=2, buttons=0x0001, flags=0x0001))
    events = drain(be, con)
    assert [e.type for e in events] == [EventType.MOUSE_DOWN, EventType.MOUSE_DRAG]
    assert (events[1].x, events[1].y) == (4.0, 2.0)


def test_bare_motion_is_dropped(backend):
    # hover is off in the profile: a terminal repaints the whole frame to show a
    # hover cue, and motion arrives for every cell crossed.
    be, con = backend
    con.push_mouse(mouse(x=2, y=2, flags=0x0001))
    assert drain(be, con) == []


def test_a_drag_burst_collapses_to_its_newest_position(backend):
    be, con = backend
    con.push_mouse(
        mouse(x=1, y=1, buttons=0x0001),
        *[mouse(x=i, y=1, buttons=0x0001, flags=0x0001) for i in range(2, 9)],
    )
    events = drain(be, con)
    drags = [e for e in events if e.type is EventType.MOUSE_DRAG]
    assert len(drags) == 1
    assert drags[0].x == 8.0


# --- mouse: wheel ---------------------------------------------------------


def test_wheel_forward_scrolls_positive(backend):
    be, con = backend
    con.push_mouse(mouse(x=3, y=3, flags=0x0004, wheel=120))
    e = drain(be, con)[0]
    assert e.type is EventType.MOUSE_SCROLL
    assert e.scroll == 1


def test_wheel_back_scrolls_negative(backend):
    # The delta is the SIGNED high word of dwButtonState; read unsigned this
    # would come back as a huge positive number and scroll the wrong way.
    be, con = backend
    con.push_mouse(mouse(flags=0x0004, wheel=-120))
    assert drain(be, con)[0].scroll == -1


def test_multi_notch_wheel_keeps_its_magnitude(backend):
    be, con = backend
    con.push_mouse(mouse(flags=0x0004, wheel=360))
    assert drain(be, con)[0].scroll == 3


def test_a_wheel_burst_sums_into_one_event(backend):
    be, con = backend
    con.push_mouse(*[mouse(flags=0x0004, wheel=120) for _ in range(5)])
    events = drain(be, con)
    assert len(events) == 1
    assert events[0].scroll == 5


def test_horizontal_wheel_reports_on_the_x_axis(backend):
    be, con = backend
    con.push_mouse(mouse(flags=0x0008, wheel=120))
    e = drain(be, con)[0]
    assert e.type is EventType.MOUSE_SCROLL
    assert e.hints.get("scroll_units_x") == 1.0


def test_modifiers_ride_along(backend):
    be, con = backend
    con.push_mouse(mouse(flags=0x0004, wheel=120, control=0x0008))  # LEFT_CTRL
    assert drain(be, con)[0].modifiers == frozenset({"ctrl"})


# --- images ---------------------------------------------------------------


def test_windows_terminal_is_detected_as_sixel(monkeypatch):
    # The one signature Windows offers: no TERM_PROGRAM, and TERM is whatever
    # the shell set.
    monkeypatch.setattr(_terminal_graphics, "have_pillow", lambda: True)
    assert _terminal_graphics.detect_protocol({"WT_SESSION": "abc"}) == "sixel"


def test_detection_still_honours_the_override(monkeypatch):
    monkeypatch.setattr(_terminal_graphics, "have_pillow", lambda: True)
    env = {"WT_SESSION": "abc", "PUIKIT_TERM_GRAPHICS": "none"}
    assert _terminal_graphics.detect_protocol(env) is None


def test_images_capability_follows_detection():
    con = FakeConsole()
    be = VTBackend(console=con)
    if be._term_graphics is None:
        assert be.capabilities["images"] is False
    else:
        assert be.capabilities["images"] is True


def test_no_protocol_means_no_placement_recorded(backend):
    be, con = backend
    be._term_graphics = None
    be.clear()
    be.draw_image(0, 0, "nonexistent.png", {"w": 4, "h": 2})
    assert be._images == {}


def test_placement_is_recorded_with_its_cell_box(backend):
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_image(3, 2, "pic.png", {"w": 6, "h": 4})
    from puikit.image import source_key

    assert be._images == {1: (3, 2, 6, 4, "pic.png", (0.0, 0.0, 1.0, 1.0),
                          (3, 2, 6, 4, (0.0, 0.0, 1.0, 1.0)),
                          source_key("pic.png"))}


def test_placement_is_clipped_to_the_enclosing_clip(backend):
    # Pixels are painted over the cells, not into them, so push_clip does not
    # trim them — the backend has to.
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.push_clip(0, 0, 5, 3)
    be.draw_image(0, 0, "pic.png", {"w": 10, "h": 8})
    be.pop_clip()
    x, y, cols, rows, _source, src, _full, _key = be._images[1]
    assert (cols, rows) == (5, 3)
    assert src[2] == pytest.approx(0.5)   # source cropped to the visible half
    assert src[3] == pytest.approx(0.375)


def test_fully_clipped_placement_is_dropped(backend):
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.push_clip(0, 0, 2, 2)
    be.draw_image(20, 20, "pic.png", {"w": 4, "h": 4})
    be.pop_clip()
    assert be._images == {}


def test_a_vanished_image_invalidates_only_its_own_cells(backend):
    # sixel has no delete verb, so the covered cells repaint over the pixels.
    # The curses backend repaints the WHOLE screen for this; only the footprint
    # should be dirtied here.
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_text(0, 0, "x" * 40)
    be.draw_text(0, 9, "keep me")
    be._images = {1: _placement(2, 1, 4, 3, "pic.png")}
    be.present()
    be.clear()                      # next frame draws no image at all
    be.draw_text(0, 0, "x" * 40)
    be.draw_text(0, 9, "keep me")
    con.written.clear()
    be.present()
    out = "".join(con.written)
    # Rows 2..4 (1-based) are re-addressed; the untouched row 10 is not.
    assert "\x1b[2;3H" in out
    assert "\x1b[10;1H" not in out


def test_unchanged_placement_is_not_retransmitted(backend):
    be, con = backend
    be._term_graphics = "sixel"
    placement = _placement(0, 0, 2, 2, "pic.png")
    be.clear()
    be._images = {1: placement}
    be.present()
    be.clear()
    be._images = {1: placement}
    con.written.clear()
    be.present()
    # No DECSC batch: nothing was re-sent.
    assert "\x1b7" not in "".join(con.written)


def test_curses_declines_images_on_windows(monkeypatch):
    # Detection finds sixel under Windows Terminal, but PDCurses writes the
    # escape to a screen buffer nobody is looking at, so the pixels never appear
    # (xefm#306). Advertising the capability would replace the Panel's alt glyph
    # — which at least shows something — with nothing.
    from puikit.backends.curses_backend import CursesBackend

    monkeypatch.delenv("PUIKIT_TERM_GRAPHICS", raising=False)
    monkeypatch.setattr(_terminal_graphics, "detect_protocol", lambda *a, **k: "sixel")
    monkeypatch.setattr("sys.platform", "win32")
    be = CursesBackend()
    assert be._term_graphics is None
    assert be.capabilities["images"] is False


def test_an_explicit_override_still_wins_on_windows(monkeypatch):
    # The suppression above is for AUTO-detection. Naming a protocol is a
    # deliberate opt-in and stays reachable, so the path can still be exercised.
    from puikit.backends.curses_backend import CursesBackend

    monkeypatch.setenv("PUIKIT_TERM_GRAPHICS", "kitty")
    monkeypatch.setattr(_terminal_graphics, "detect_protocol", lambda *a, **k: "kitty")
    monkeypatch.setattr("sys.platform", "win32")
    be = CursesBackend()
    assert be._term_graphics == "kitty"


def test_curses_still_takes_images_off_windows(monkeypatch):
    from puikit.backends.curses_backend import CursesBackend

    monkeypatch.setattr(_terminal_graphics, "detect_protocol", lambda *a, **k: "kitty")
    monkeypatch.setattr("sys.platform", "linux")
    be = CursesBackend()
    assert be._term_graphics == "kitty"
    assert be.capabilities["images"] is True


def test_an_overpainted_image_is_resent_even_though_it_did_not_move(backend, png):
    # ImageButton: clicking restyles the cells under the picture. The placement
    # is unchanged, so the change-diff alone would skip it — and the text the
    # frame re-sends lands on top of the pixels and erases them.
    be, con = backend
    be._term_graphics = "sixel"
    placement = _placement(0, 0, 6, 3, png)
    be.clear()
    be.draw_text(0, 0, "button")
    be._images = {1: placement}
    be.present()
    be.clear()
    be.draw_text(0, 0, "BUTTON")     # pressed styling: same box, different cells
    be._images = {1: placement}
    con.written.clear()
    be.present()
    assert "\x1b7" in "".join(con.written)  # the image batch went out again


def test_an_untouched_image_is_still_not_resent(backend, png):
    # The counterpart: re-sending on every frame would make scrolling crawl, so
    # only genuinely overpainted placements pay.
    be, con = backend
    be._term_graphics = "sixel"
    placement = _placement(0, 0, 4, 2, png)
    be.clear()
    be.draw_text(0, 8, "far away")
    be._images = {1: placement}
    be.present()
    be.clear()
    be.draw_text(0, 8, "FAR AWAY")   # changes a row nowhere near the image
    be._images = {1: placement}
    con.written.clear()
    be.present()
    assert "\x1b7" not in "".join(con.written)


def test_a_first_placement_is_emitted_at_its_cell(backend, png):
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be._images = {1: _placement(4, 2, 3, 2, png)}
    con.written.clear()
    be.present()
    out = "".join(con.written)
    assert "\x1b7" in out and "\x1b8" in out   # cursor saved/restored around it
    assert "\x1b[3;5H" in out                  # addressed absolutely, 1-based


def test_erasing_a_stale_image_also_clears_the_row_below(backend):
    # A protocol paints pixels, not cells, and its own rounding can put a few of
    # them just outside the box. Those land in a row whose text did not change,
    # so the frame diff would never repaint it and the spill stays on screen —
    # scrolling an image upward leaves a stripe behind on every step.
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_text(0, 0, "x" * 20)
    be.draw_text(0, 5, "row below the image")
    be._images = {1: _placement(0, 1, 4, 3, "pic.png")}
    be.present()
    be.clear()                       # next frame: the image is gone
    be.draw_text(0, 0, "x" * 20)
    be.draw_text(0, 5, "row below the image")
    con.written.clear()
    be.present()
    out = "".join(con.written)
    # Row 5 (1-based) is one past the footprint's last row (rows 2..4) and must
    # be re-sent even though its text is unchanged.
    assert "\x1b[5;1H" in out, out[:200]


# --- decoded pixels as a source -------------------------------------------
#
# The other thing draw_image takes: a RasterImage the application decoded
# itself, for a format no backend reads. What is worth checking on this side is
# not the picture (test_raster_image covers the decode) but the diff: a raster
# is the one source that can change *without* the placement changing, because it
# is the same object holding different bytes.

def test_a_raster_is_drawn_without_a_file_anywhere(backend):
    from puikit.image import RasterImage

    pytest.importorskip("PIL")
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_image(0, 0, RasterImage(4, 4, bytes((200, 30, 30, 255)) * 16),
                  {"w": 4, "h": 2})
    con.written.clear()
    be.present()
    assert "\x1b7" in "".join(con.written)


def test_a_repainted_raster_is_re_emitted_where_it_stands(backend):
    # The case a placement tuple alone cannot see: same object, same position,
    # different pixels. Without the source's revision in the tuple the frame
    # compares equal and the screen keeps the picture from before the edit.
    from puikit.image import RasterImage

    pytest.importorskip("PIL")
    be, con = backend
    be._term_graphics = "sixel"

    raster = RasterImage(4, 4, bytes((200, 30, 30, 255)) * 16)
    for _ in range(2):
        be.clear()
        be.draw_image(0, 0, raster, {"w": 4, "h": 2})
        be.present()

    con.written.clear()
    be.clear()
    raster.update(4, 4, bytes((30, 30, 200, 255)) * 16)   # painted into
    be.draw_image(0, 0, raster, {"w": 4, "h": 2})
    be.present()
    assert "\x1b7" in "".join(con.written)


def test_a_still_raster_is_not_re_emitted(backend):
    # The counterpart: a raster nobody wrote to costs nothing per frame, or a
    # viewer showing one would re-encode it forever.
    from puikit.image import RasterImage

    pytest.importorskip("PIL")
    be, con = backend
    be._term_graphics = "sixel"

    raster = RasterImage(4, 4, bytes((200, 30, 30, 255)) * 16)
    for _ in range(2):
        be.clear()
        be.draw_image(0, 0, raster, {"w": 4, "h": 2})
        be.present()

    con.written.clear()
    be.clear()
    be.draw_image(0, 0, raster, {"w": 4, "h": 2})
    be.present()
    assert "\x1b7" not in "".join(con.written)


def test_a_file_rewritten_under_the_same_name_is_re_emitted(backend, png, tmp_path):
    # The same gap on the path side, and the reason source_key stats the file
    # rather than trusting its name: a thumbnail refreshed in place, or a build
    # artifact rewritten, is a different picture at an unchanged placement.
    from PIL import Image

    be, con = backend
    be._term_graphics = "sixel"
    for _ in range(2):
        be.clear()
        be.draw_image(0, 0, png, {"w": 4, "h": 2})
        be.present()

    Image.new("RGB", (40, 30), (10, 10, 200)).save(png)
    con.written.clear()
    be.clear()
    be.draw_image(0, 0, png, {"w": 4, "h": 2})
    be.present()
    assert "\x1b7" in "".join(con.written)


# --- an overlay drawn over a picture ---------------------------------------
#
# The pixels go out after the whole grid, because the frame's own text would
# otherwise land on top of them. Without a rule saying what is in front of
# what, that puts every picture in front of everything: a help overlay opened
# over the image viewer reads as being BEHIND the picture, and its own cells
# then make the placement look overpainted, so it is re-sent and buries them
# again (xefm#458). The rule is the one a compositing backend gets for free —
# whatever is drawn after the image is in front of it.


def _boxes(be):
    return sorted(p[:4] for p in be._placements.values())


def test_a_picture_nothing_covers_is_still_one_placement(backend, png):
    be, _con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_image(2, 1, png, {"w": 10, "h": 6})
    be.present()
    assert _boxes(be) == [(2, 1, 10, 6)]


def test_an_overlay_drawn_after_the_picture_is_cut_out_of_it(backend, png):
    be, _con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_image(2, 1, png, {"w": 10, "h": 6})
    for row in (3, 4):
        be.draw_text(4, row, "    ")     # a dialog's own background fill
    be.present()
    assert _boxes(be) == [(2, 1, 10, 2), (2, 3, 2, 2), (2, 5, 10, 2), (8, 3, 4, 2)]


def test_text_drawn_before_the_picture_stays_under_it(backend, png):
    # The ImageButton case: the cells beneath the picture are painted first and
    # stay beneath it, so the placement is still one box.
    be, _con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_text(2, 2, "button")
    be.draw_image(2, 1, png, {"w": 10, "h": 6})
    be.present()
    assert _boxes(be) == [(2, 1, 10, 6)]


def test_a_surviving_part_carries_the_matching_part_of_the_picture(backend, png):
    be, _con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_image(0, 0, png, {"w": 10, "h": 4})
    be.draw_text(0, 2, "  ")             # the left two columns of one row
    be.present()
    src = {p[:4]: p[5] for p in be._placements.values()}
    assert src[(0, 0, 10, 2)] == pytest.approx((0.0, 0.0, 1.0, 0.5))
    assert src[(2, 2, 8, 1)] == pytest.approx((0.2, 0.5, 0.8, 0.25))


def test_a_settled_overlay_does_not_ask_for_the_picture_back(backend, png):
    # The second half of the bug. The covered cells are no longer part of any
    # placement, so they cannot report the picture as overpainted.
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_image(0, 0, png, {"w": 12, "h": 6})
    for row in (2, 3):
        be.draw_text(3, row, "help")
    be.present()
    be.clear()
    be.draw_image(0, 0, png, {"w": 12, "h": 6})
    for row in (2, 3):
        be.draw_text(3, row, "help")
    con.written.clear()
    be.present()
    assert "\x1b7" not in "".join(con.written)


def test_closing_the_overlay_brings_the_whole_picture_back(backend, png):
    be, _con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_image(0, 0, png, {"w": 12, "h": 6})
    for row in (2, 3):
        be.draw_text(3, row, "help")
    be.present()
    be.clear()
    be.draw_image(0, 0, png, {"w": 12, "h": 6})
    be.present()
    assert _boxes(be) == [(0, 0, 12, 6)]


def test_an_uncovered_picture_resolves_to_what_was_recorded(backend, png):
    # The cost of the rule on the common case, stated as an identity: nothing
    # drawn over the picture means the resolved placement IS the recorded one,
    # down to the id — same cache key, same payload, same diff.
    be, _con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.draw_image(1, 1, png, {"w": 8, "h": 4})
    be.present()
    assert be._placements == be._images
