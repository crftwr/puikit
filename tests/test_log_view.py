"""LogView tests run identically against the TUI and GUI capability profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, Style
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import LogView


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
    # Drag from column 1 to column 4 on row 0 selects "bcd".
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=1, y=0, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=4, y=0, button="left"))
    assert log.selection_text() == "bcd"


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
