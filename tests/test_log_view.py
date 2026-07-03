"""LogView tests run identically against the TUI and GUI capability profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, Style
from puikit.backends.memory_backend import MemoryBackend
from puikit.text import display_width, wrap_text
from puikit.widgets import LogView
from puikit.widgets.log_view import wrap_columns


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
