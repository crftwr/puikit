"""LogView tests run identically against the TUI and GUI capability profiles."""

import pytest

from puikit import (
    CapabilityProfile,
    Event,
    EventType,
    Font,
    Panel,
    PROFILE_GUI_DESKTOP,
    PROFILE_TUI,
    Style,
)
from puikit.backend import DEFAULT_STYLE
from puikit.backends.memory_backend import MemoryBackend
from puikit.text import display_width, wrap_text
from puikit.widgets import LogView, _input
from puikit.widgets.log_view import wrap_columns

# Neither composited animation nor timed ticks: the "still" backend an edge
# auto-scroll has to degrade on.
PROFILE_STILL = CapabilityProfile({**PROFILE_TUI, "animation_ticks": False})


@pytest.mark.parametrize(
    "text",
    [
        "",
        "short",
        "the quick brown fox jumps over the lazy dog again and again",
        "supercalifragilisticexpialidocious-is-one-very-long-unbreakable-token",
        "trailing   spaces   between   words   here   too",
        "日本語のテキストは空白を使わないため文字単位で折り返す",  # wide CJK, no spaces
        "mixed 日本語 and ascii words 折り返し test line",
        "emoji ⚠️ warning 🫧 bubble run with selectors",
    ],
)
@pytest.mark.parametrize("width", [1, 5, 8, 12, 20])
@pytest.mark.parametrize("word", [True, False])
def test_wrap_columns_matches_wrap_text(text, width, word):
    # wrap_columns is the O(n) fast path for grid (font=None) text; it must
    # produce byte-identical output to the canonical wrap_text driven by the
    # column measure (display_width), or a wrapped log would diverge from the
    # rest of the framework's text handling.
    expected = wrap_text(text, float(width), display_width, word=word)
    assert wrap_columns(text, width, word=word) == expected


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=20, height=5, capabilities=request.param)


def test_logview_renders_visible_slice(backend):
    panel = Panel(backend)
    log = LogView([f"line{i}" for i in range(20)], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    lines = backend.snapshot()
    assert lines[0].startswith("line0")
    assert lines[4].startswith("line4")


def test_logview_virtualizes_large_buffer(backend):
    # 10k lines must not wrap or touch every row: only the visible window is
    # drawn. We assert correctness of the visible slice; the point is it stays
    # cheap, which the virtualized draw loop guarantees.
    panel = Panel(backend)
    log = LogView([f"line{i}" for i in range(10000)])  # auto_scroll on by default
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    # Following the tail: the last 5 lines are shown.
    assert backend.snapshot()[4].startswith("line9999")


def test_logview_follows_tail_on_append(backend):
    panel = Panel(backend)
    log = LogView([f"line{i}" for i in range(10)])
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    assert backend.snapshot()[4].startswith("line9")
    log.append("fresh")
    panel.render()
    assert backend.snapshot()[4].startswith("fresh")


def test_logview_stops_following_after_scroll_up(backend):
    panel = Panel(backend)
    log = LogView([f"line{i}" for i in range(20)])
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_SCROLL, x=1, y=1, scroll=3))  # up
    assert not log._follow
    log.append("fresh")
    panel.render()
    # The viewport stayed where the user left it, not pinned to the new tail.
    assert not backend.snapshot()[4].startswith("fresh")


def test_logview_per_line_color(backend):
    red = Style(fg=(205, 49, 49))
    panel = Panel(backend)
    log = LogView([("plain", Style()), ("warn", red)], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    assert backend.style_at(0, 1).fg == (205, 49, 49)
    assert backend.style_at(0, 0).fg != (205, 49, 49)


def test_logview_wrapping_grows_display_rows(backend):
    panel = Panel(backend)
    # One logical line far wider than the 20-col pane folds into several rows.
    log = LogView(["word " * 12], wrap="word", auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    assert log._total_rows > 1
    # The second display row is a continuation of the same logical line.
    assert backend.snapshot()[1].strip().startswith("word")


def test_logview_select_all_and_copy(backend):
    panel = Panel(backend)
    log = LogView(["alpha", "beta"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.KEY, key="a", modifiers=frozenset({"ctrl"})))
    panel.dispatch_event(Event(type=EventType.KEY, key="c", modifiers=frozenset({"ctrl"})))
    assert panel.get_clipboard() == "alpha\nbeta"


def test_logview_selection_color_tracks_focus(backend):
    panel = Panel(backend)
    log = LogView(["alpha"], auto_scroll=False)
    other = LogView(["x"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=2)
    panel.add(other, x=0, y=3, w=20, h=1)
    panel.render()
    panel.dispatch_event(Event(type=EventType.KEY, key="a", modifiers=frozenset({"ctrl"})))
    panel.render()
    assert backend.style_at(0, 0).bg == panel.theme.text_selection_bg  # focused
    # Focus moves away: the highlight stays but reads as inactive.
    panel.focus_tab(1)
    panel.render()
    assert backend.style_at(0, 0).bg == panel.theme.text_selection_inactive_bg


def test_logview_drag_selection_copies_visible_text(backend):
    panel = Panel(backend)
    log = LogView(["abcdef"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    # Press at column 1, drag to column 4 on row 0: selects "bcd".
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=4, y=0, button="left"))
    assert log.selection_text() == "bcd"


def test_logview_outside_press_drag_in_does_not_select(backend):
    panel = Panel(backend)
    log = LogView(["abcdef"], auto_scroll=False)
    # Leave columns 0-2 as empty panel space to the left of the view.
    panel.add(log, x=3, y=0, w=10, h=5)
    panel.render()
    # Press on empty space (no widget captures it), then drag across the view:
    # the gesture did not begin in the view, so it must not start a selection
    # (two drag points, so a missing guard would leave a non-empty range).
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=5, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=9, y=0, button="left"))
    assert log.selection_text() == ""


def test_logview_press_seeds_anchor_at_press_point(backend):
    panel = Panel(backend)
    log = LogView(["abcdef"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    # A completed selection somewhere, then a fresh press elsewhere: the new
    # press must reseed the anchor at the press point, so a following drag does
    # not start from the stale anchor of the previous gesture (the reported bug).
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=2, y=0, button="left"))
    assert log.selection_text() == "ab"
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=3, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=5, y=0, button="left"))
    assert log.selection_text() == "de"


def test_logview_plain_press_clears_selection(backend):
    panel = Panel(backend)
    log = LogView(["abcdef"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=4, y=0, button="left"))
    assert log.selection_text() == "bcd"
    # A plain press with no drag collapses anchor onto cursor: nothing selected.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=2, y=0, button="left"))
    assert log.selection_text() == ""


def test_logview_double_click_selects_word(backend):
    panel = Panel(backend)
    log = LogView(["foo bar baz"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    # Two presses in place on "bar" grab the whole word, not the surrounding
    # spaces.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    assert log.selection_text() == "bar"


def test_logview_triple_click_selects_line(backend):
    panel = Panel(backend)
    log = LogView(["foo bar baz"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    for _ in range(3):
        panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    assert log.selection_text() == "foo bar baz"


def test_logview_fourth_click_wraps_back_to_caret(backend):
    panel = Panel(backend)
    log = LogView(["foo bar baz"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    # caret -> word -> line -> caret: a fourth press collapses the selection.
    for _ in range(4):
        panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    assert log.selection_text() == ""


def test_logview_double_click_drag_extends_by_word(backend):
    panel = Panel(backend)
    log = LogView(["foo bar baz"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    # Double-click "foo", then drag into "baz": whole-word edges are kept, so
    # the whole span "foo bar baz" is taken even though the drag ends mid-word.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=0, button="left"))
    assert log.selection_text() == "foo"
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=9, y=0, button="left"))
    assert log.selection_text() == "foo bar baz"


def test_logview_drag_after_press_is_not_a_double_click(backend):
    panel = Panel(backend)
    log = LogView(["foo bar baz"], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    # A drag between two presses breaks the multi-click run: the second press is
    # a fresh caret, not a word selection.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=6, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=5, y=0, button="left"))
    assert log.selection_text() == ""


def test_logview_max_lines_trims_oldest(backend):
    panel = Panel(backend)
    log = LogView(max_lines=100, auto_scroll=False)
    for i in range(400):
        log.append(f"line{i}")
    # Trimming is batched but must keep the buffer bounded and drop the oldest.
    assert len(log.lines) <= 100 + max(64, 100 // 8)
    assert log.lines[-1][0] == "line399"
    assert log.lines[0][0] != "line0"


def test_logview_keyboard_scrolls_viewport(backend):
    panel = Panel(backend)
    log = LogView([f"line{i}" for i in range(20)], auto_scroll=False)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    assert backend.snapshot()[0].startswith("line0")
    panel.dispatch_event(Event(type=EventType.KEY, key="end"))
    panel.render()
    assert backend.snapshot()[4].startswith("line19")
    panel.dispatch_event(Event(type=EventType.KEY, key="home"))
    panel.render()
    assert backend.snapshot()[0].startswith("line0")


def test_logview_padding_arithmetic():
    # padding_units applies everywhere; padding_px is a sub-cell fraction of the
    # base size, expressed only on a pixel/vector backend and collapsing on a grid.
    log = LogView(padding_px=4, padding_units=1)
    assert log._padding(True, 8, 16) == (1.5, 1.25)   # 1 cell + 4/8, 1 cell + 4/16
    assert log._padding(False, 8, 16) == (1.0, 1.0)   # grid: only the whole cells
    assert LogView(padding_px=4)._padding(False, 8, 16) == (0.0, 0.0)


def test_logview_padding_insets_rows_and_shrinks_viewport(backend):
    # A 1-cell pad on every side: rows shift one column right and one row down,
    # and the visible viewport shrinks by the top+bottom pad.
    panel = Panel(backend)
    log = LogView([f"line{i}" for i in range(20)], auto_scroll=False, padding_units=1)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    snap = backend.snapshot()
    assert snap[0].strip() == ""            # top pad row
    assert snap[1].startswith(" line0")     # inset one column, first content row
    assert snap[3].startswith(" line2")     # view_h shrank 5 -> 3, so lines 0..2
    assert snap[4].strip() == ""            # bottom pad row
    assert log._view_h == 3.0


class SizedFontMemoryBackend(MemoryBackend):
    """MemoryBackend with GUI-style font measuring — a sized font's advance
    scales with its point size relative to the base font, like the real GUI
    backends — and a record of every draw_text run, so a test can assert what
    a widget actually handed the backend (the character grid can't retain a
    sub-column glyph)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.drawn: list[tuple[float, float, str]] = []

    def _scale(self, style: Style) -> float:
        font = style.font
        if font is not None and font.size is not None:
            return font.size / self.BASE_FONT_SIZE
        return 1.0

    def measure_text(self, text: str, style: Style = DEFAULT_STYLE) -> float:
        return float(display_width(text)) * self._scale(style)

    def measure_line_height(self, style: Style = DEFAULT_STYLE) -> float:
        return self._scale(style)

    def draw_text(self, x, y, text, style: Style = DEFAULT_STYLE) -> None:
        self.drawn.append((x, y, text))
        super().draw_text(x, y, text, style)


# Half the 14pt base size: exactly two glyphs per base unit, so the expected
# glyph counts below stay whole numbers.
_SMALL = Style(font=Font(size=7.0, monospace=True))


def _sized_font_view(text, *, wrap, width=10):
    backend = SizedFontMemoryBackend(width=20, height=10, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    log = LogView([(text, _SMALL)], wrap=wrap, auto_scroll=False)
    panel.add(log, x=0, y=0, w=width, h=10)
    panel.render()
    return backend, panel, log


def test_logview_sized_font_wrap_keeps_every_glyph():
    # A row styled with a smaller real font packs more glyphs than the pane
    # has columns; the draw must clip it by the same measure the wrap used.
    # Clipping by column count instead cut the tail off every full row (the
    # reported bug: an 11pt log under a 12pt base lost ~9% of each row).
    text = "abcdefghijklmnopqrstuvwxyz0123456789"  # one unbreakable 36-glyph token
    backend, _panel, log = _sized_font_view(text, wrap="word")
    # Wrap width 9 units (10 - the reserved gutter) holds 18 half-unit glyphs.
    assert log._total_rows == 2
    assert "".join(t for _x, _y, t in backend.drawn) == text


def test_logview_sized_font_unwrapped_clips_by_measure():
    # Unwrapped, the one row must clip at the pane's measured width — 20
    # half-unit glyphs across 10 base units — not at 10 grid columns.
    text = "abcdefghijklmnopqrst" + "!" * 20
    backend, _panel, log = _sized_font_view(text, wrap=False)
    assert log._total_rows == 1
    assert backend.drawn == [(0.0, 0.0, text[:20])]


def test_logview_sized_font_click_maps_by_measure():
    # Pointer hit-testing must use the same half-unit advance the glyphs are
    # drawn with: x=5 base units is 10 glyphs in, not 5.
    text = "abcdefghijklmnopqrst"
    _backend, panel, log = _sized_font_view(text, wrap=False)
    assert log._pos_at(5, 0) == (0, 10)
    # And the full gesture agrees: press at x=1 (glyph 2), drag to x=4
    # (glyph 8) selects the six glyphs between them.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=4, y=0, button="left"))
    assert log.selection_text() == "cdefgh"


def test_logview_padding_maps_clicks_through_the_inset(backend):
    # A pointer hit undoes the same inset the rows were drawn with, so a click on
    # a padded row selects that row, not the one a raw y would have hit.
    panel = Panel(backend)
    log = LogView([f"line{i}" for i in range(20)], auto_scroll=False, padding_units=1)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    # Screen row 2 with a 1-row top pad maps to content row 1.
    assert log._pos_at(2, 2)[0] == 1
    # Column undo: screen col 1 is the row's first glyph (col 0) after the pad.
    assert log._pos_at(1, 1) == (0, 0)


# --- drag past the edge auto-scrolls -----------------------------------------
#
# A selection drag held outside the view keeps scrolling it, so a selection can
# run past the one screenful the pointer can reach. The scroll is time-based
# (one rate on a 60fps GUI tick and a terminal's slower one), so these tests
# drive a fake clock rather than real elapsed time.


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(_input.time, "monotonic", c)
    return c


def _edge_drag_view(backend, lines=60, **kwargs):
    panel = Panel(backend)
    log = LogView([f"line{i}" for i in range(lines)], auto_scroll=False, **kwargs)
    panel.add(log, x=0, y=0, w=20, h=5)
    panel.render()
    return panel, log


def test_logview_drag_below_the_view_scrolls_and_extends(backend, clock):
    panel, log = _edge_drag_view(backend)
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    # Drag well below the pane: the Panel clamps the coordinate onto the bottom
    # edge, so the selection alone would stop at the last visible row.
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=6, y=40, button="left"))
    assert log._sel_cursor[0] == 4  # the last row on screen, for now
    for _ in range(10):
        clock.advance(1 / 60)
        backend.run_animation_ticks()
    # A sixth of a second past the edge has brought further rows into view, and
    # the selection grew with them rather than stopping at the old bottom row.
    assert log.offset > 0
    assert log._sel_cursor[0] > 4
    assert log.selection_text().startswith("line0\nline1")
    assert log.selection_text().splitlines()[-1].startswith("line")


def test_logview_drag_above_the_view_scrolls_back_up(backend, clock):
    panel, log = _edge_drag_view(backend)
    log.scroll_by(30.0)  # start part-way down the buffer
    panel.render()
    start = log.offset
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=4, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=0, y=-8, button="left"))
    for _ in range(10):
        clock.advance(1 / 60)
        backend.run_animation_ticks()
    assert log.offset < start
    assert log.selection_text() != ""


def test_logview_edge_scroll_speed_ramps_with_distance(backend, clock):
    # Further past the edge scrolls faster: the same elapsed time covers more
    # rows. Without the ramp, selecting through a long log means waiting at one
    # fixed rate no matter how far out the pointer is pulled.
    def travelled(y):
        _panel, log = _edge_drag_view(backend)
        _panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
        _panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=0, y=y, button="left"))
        clock.advance(0.25)
        backend.run_animation_ticks()
        return log.offset

    assert travelled(20) > travelled(6) > 0


def test_logview_edge_scroll_stops_at_the_end_of_the_buffer(backend, clock):
    panel, log = _edge_drag_view(backend, lines=8)
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=0, y=40, button="left"))
    for _ in range(50):
        clock.advance(1 / 60)
        backend.run_animation_ticks()
    # Hard against the bottom: the whole buffer is selected and the timer has
    # retired itself rather than re-rendering every frame for nothing.
    assert log.offset == pytest.approx(log._content_h - log._view_h)
    assert log.selection_text().endswith("line7")
    assert backend.tick_callbacks == []


def test_logview_drag_inside_the_view_does_not_scroll(backend, clock):
    panel, log = _edge_drag_view(backend)
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=4, y=3, button="left"))
    for _ in range(10):
        clock.advance(1 / 60)
        backend.run_animation_ticks()
    assert log.offset == 0.0
    # Inside, the pointer's column still decides the endpoint (x=4 is 4 glyphs
    # into row 3) — the whole-row rule applies only past an edge.
    assert log.selection_text() == "line0\nline1\nline2\nline"


def test_logview_release_stops_the_edge_scroll(backend, clock):
    panel, log = _edge_drag_view(backend)
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=0, y=40, button="left"))
    clock.advance(1 / 60)
    backend.run_animation_ticks()
    panel.dispatch_event(Event(type=EventType.MOUSE_UP, x=0, y=40, button="left"))
    settled = (log.offset, log._sel_cursor)
    for _ in range(10):
        clock.advance(1 / 60)
        backend.run_animation_ticks()
    assert (log.offset, log._sel_cursor) == settled
    assert backend.tick_callbacks == []


def test_logview_edge_scroll_steps_per_event_without_a_timer(clock):
    # A backend with no animation ticks at all still scrolls — one step per drag
    # event, so a moving drag works and only a held-still one stalls.
    still = MemoryBackend(width=20, height=5, capabilities=PROFILE_STILL)
    panel, log = _edge_drag_view(still)
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=0, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=0, y=40, button="left"))
    assert log.offset > 0
    assert still.tick_callbacks == []


def test_logview_seeded_over_max_lines_trims_on_construction():
    # Trimming resets the selection and the wrap cache, so a buffer seeded past
    # its own cap has to find that state already built.
    log = LogView([f"line{i}" for i in range(500)], max_lines=100)
    assert len(log.lines) == 100
    assert log.lines[-1] == ("line499", log.style)
