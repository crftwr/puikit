"""Mouse input and inline images on the VT backend.

Windows reports mouse STATE, not gestures: every record says which buttons are
down now, so press / release / drag come from comparing against the previous
record. That is the part worth testing — the translation, not the ctypes.

Images matter here for a different reason. The curses backend already
implements them; they never appear on Windows because PDCurses displays a
different screen buffer than the one the escape is written to (xefm#306). Owning
the output stream is the whole fix, so what these check is that the payload is
emitted, positioned, and erased at the right moments.
"""

import io

import pytest

from puikit.backends import _terminal_graphics
from puikit.backends.vt_backend import VTBackend, _StreamConsole
from puikit.event import EventType


class FakeConsole(_StreamConsole):
    def __init__(self, width=40, height=10):
        super().__init__(stream=io.StringIO(), size=(width, height))
        self._fixed = (width, height)
        self.queue: list[list[dict]] = []

    def size(self):
        return self._fixed

    def read_input(self, timeout_ms):
        return self.queue.pop(0) if self.queue else []


def mouse(x=0, y=0, buttons=0, flags=0, wheel=0, control=0):
    return {"type": "mouse", "x": x, "y": y, "buttons": buttons,
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
    con.queue.append([mouse(x=5, y=3, buttons=0x0001), mouse(x=5, y=3, buttons=0)])
    events = drain(be, con)
    assert [e.type for e in events] == [EventType.MOUSE_DOWN, EventType.MOUSE_UP]
    assert all(e.button == "left" for e in events)
    assert (events[0].x, events[0].y) == (5.0, 3.0)


def test_right_and_middle_buttons_are_named(backend):
    be, con = backend
    con.queue.append([mouse(buttons=0x0002), mouse(buttons=0),
                      mouse(buttons=0x0004), mouse(buttons=0)])
    buttons = [e.button for e in drain(be, con)]
    assert buttons == ["right", "right", "middle", "middle"]


def test_no_event_when_state_is_unchanged(backend):
    # Windows re-reports the same state freely; only transitions are gestures.
    be, con = backend
    con.queue.append([mouse(buttons=0x0001), mouse(buttons=0x0001)])
    assert [e.type for e in drain(be, con)] == [EventType.MOUSE_DOWN]


# --- mouse: motion --------------------------------------------------------


def test_motion_with_a_button_held_is_a_drag(backend):
    be, con = backend
    con.queue.append([mouse(x=1, y=1, buttons=0x0001),
                      mouse(x=4, y=2, buttons=0x0001, flags=0x0001)])
    events = drain(be, con)
    assert [e.type for e in events] == [EventType.MOUSE_DOWN, EventType.MOUSE_DRAG]
    assert (events[1].x, events[1].y) == (4.0, 2.0)


def test_bare_motion_is_dropped(backend):
    # hover is off in the profile: a terminal repaints the whole frame to show a
    # hover cue, and motion arrives for every cell crossed.
    be, con = backend
    con.queue.append([mouse(x=2, y=2, flags=0x0001)])
    assert drain(be, con) == []


def test_a_drag_burst_collapses_to_its_newest_position(backend):
    be, con = backend
    con.queue.append(
        [mouse(x=1, y=1, buttons=0x0001)]
        + [mouse(x=i, y=1, buttons=0x0001, flags=0x0001) for i in range(2, 9)]
    )
    events = drain(be, con)
    drags = [e for e in events if e.type is EventType.MOUSE_DRAG]
    assert len(drags) == 1
    assert drags[0].x == 8.0


# --- mouse: wheel ---------------------------------------------------------


def test_wheel_forward_scrolls_positive(backend):
    be, con = backend
    con.queue.append([mouse(x=3, y=3, flags=0x0004, wheel=120)])
    e = drain(be, con)[0]
    assert e.type is EventType.MOUSE_SCROLL
    assert e.scroll == 1


def test_wheel_back_scrolls_negative(backend):
    # The delta is the SIGNED high word of dwButtonState; read unsigned this
    # would come back as a huge positive number and scroll the wrong way.
    be, con = backend
    con.queue.append([mouse(flags=0x0004, wheel=-120)])
    assert drain(be, con)[0].scroll == -1


def test_multi_notch_wheel_keeps_its_magnitude(backend):
    be, con = backend
    con.queue.append([mouse(flags=0x0004, wheel=360)])
    assert drain(be, con)[0].scroll == 3


def test_a_wheel_burst_sums_into_one_event(backend):
    be, con = backend
    con.queue.append([mouse(flags=0x0004, wheel=120) for _ in range(5)])
    events = drain(be, con)
    assert len(events) == 1
    assert events[0].scroll == 5


def test_horizontal_wheel_reports_on_the_x_axis(backend):
    be, con = backend
    con.queue.append([mouse(flags=0x0008, wheel=120)])
    e = drain(be, con)[0]
    assert e.type is EventType.MOUSE_SCROLL
    assert e.hints.get("scroll_units_x") == 1.0


def test_modifiers_ride_along(backend):
    be, con = backend
    con.queue.append([mouse(flags=0x0004, wheel=120, control=0x0008)])  # LEFT_CTRL
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
    assert be._images == {1: (3, 2, 6, 4, "pic.png", (0.0, 0.0, 1.0, 1.0))}


def test_placement_is_clipped_to_the_enclosing_clip(backend):
    # Pixels are painted over the cells, not into them, so push_clip does not
    # trim them — the backend has to.
    be, con = backend
    be._term_graphics = "sixel"
    be.clear()
    be.push_clip(0, 0, 5, 3)
    be.draw_image(0, 0, "pic.png", {"w": 10, "h": 8})
    be.pop_clip()
    x, y, cols, rows, _path, src = be._images[1]
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
    be._images = {1: (2, 1, 4, 3, "pic.png", None)}
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
    placement = (0, 0, 2, 2, "pic.png", None)
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
    placement = (0, 0, 6, 3, png, None)
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
    placement = (0, 0, 4, 2, png, None)
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
    be._images = {1: (4, 2, 3, 2, png, None)}
    con.written.clear()
    be.present()
    out = "".join(con.written)
    assert "\x1b7" in out and "\x1b8" in out   # cursor saved/restored around it
    assert "\x1b[3;5H" in out                  # addressed absolutely, 1-based
