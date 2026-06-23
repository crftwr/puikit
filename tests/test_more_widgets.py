"""Tests for ProgressBar, BusyIndicator, Splitter and ComboBox, run against
the TUI and GUI profiles alike."""

import pytest

from puikit import CapabilityProfile, Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import (
    BusyIndicator,
    Checkbox,
    ComboBox,
    Label,
    ProgressBar,
    Splitter,
)


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=40, height=16, capabilities=request.param)


def _key(name, char=None, modifiers=frozenset()):
    return Event(type=EventType.KEY, key=name, char=char, modifiers=modifiers)


def _click(x, y, button="left"):
    return Event(type=EventType.MOUSE_CLICK, x=x, y=y, button=button)


# --- ProgressBar -------------------------------------------------------------


def test_progressbar_fills_fraction_with_accent(backend):
    panel = Panel(backend)
    bar = ProgressBar(0.5)
    panel.add(bar, x=0, y=0, w=20, h=1)
    panel.render()
    # On the grid the bar is a thin centered rule, not a filled band: the left
    # half is the heavy accent glyph, the right half the light track glyph.
    row = backend.snapshot()[0]
    assert row[2] == "━" and backend.style_at(2, 0).fg == panel.theme.accent
    assert row[18] == "─" and backend.style_at(18, 0).fg == panel.theme.control_border


def test_progressbar_clamps_value(backend):
    panel = Panel(backend)
    bar = ProgressBar(2.0)  # over-full
    panel.add(bar, x=0, y=0, w=20, h=1)
    panel.render()
    # Fully filled: even the far end is the accent rule.
    assert backend.style_at(19, 0).fg == panel.theme.accent
    bar.value = -1.0  # under-empty
    panel.render()
    assert backend.style_at(0, 0).fg == panel.theme.control_border


def test_progressbar_measures_one_line_high_and_fills_width(backend):
    from puikit.layout import LayoutContext

    bar = ProgressBar(0.3)
    lc = LayoutContext(8, 16, snap=True)
    assert bar.measure(lc, "y", 20).preferred == 1.0
    assert bar.measure(lc, "x", 1).preferred == 0.0  # no opinion -> fills


# --- BusyIndicator -----------------------------------------------------------


def test_busyindicator_draws_a_frame_and_label(backend):
    panel = Panel(backend)
    spin = BusyIndicator("Loading")
    panel.add(spin, x=0, y=0, w=20, h=1)
    panel.render()
    line = backend.snapshot()[0]
    assert line[0] in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    assert "Loading" in line


def test_busyindicator_ticks_only_on_animation_backend(backend):
    panel = Panel(backend)
    spin = BusyIndicator()
    panel.add(spin, x=0, y=0, w=8, h=1)
    panel.render()
    # A backend that drives ticks is one with rich animation (GUI) or just timed
    # re-render ticks (TUI's timer-woken event loop, capability animation_ticks).
    caps = backend.capabilities
    animated = caps.supports("animation") or caps.supports("animation_ticks")
    assert bool(backend.tick_callbacks) is animated
    # Stopping ends the tick on the next round (tick-driving backends only).
    if animated:
        spin.stop()
        backend.run_animation_ticks()
        assert backend.tick_callbacks == []


def test_busyindicator_unregisters_tick_when_detached(backend):
    # A spinner whose page is swapped out stops being drawn. Its tick must
    # self-unregister so it neither pins the detached widget alive nor keeps
    # driving off-screen re-renders forever (the demo-catalog memory leak).
    caps = backend.capabilities
    if not (caps.supports("animation") or caps.supports("animation_ticks")):
        pytest.skip("still backend never registers a tick")
    panel = Panel(backend)
    spin = BusyIndicator()
    panel.add(spin, x=0, y=0, w=8, h=1)
    panel.render()
    assert backend.tick_callbacks  # registered while drawn
    # Detach the spinner from the panel: it is no longer drawn on render.
    panel.remove(spin)
    panel.render()
    backend.run_animation_ticks()  # last tick that still saw _drawn from before
    backend.run_animation_ticks()  # now no draw intervened -> unregisters
    assert backend.tick_callbacks == []


def test_busyindicator_intrinsic_width_covers_glyph_and_label(backend):
    from puikit.layout import LayoutContext

    lc = LayoutContext(8, 16, snap=True)
    plain = BusyIndicator()
    assert plain.measure(lc, "x", 1).preferred == 1.0  # one glyph cell
    labeled = BusyIndicator("Sync")
    assert labeled.measure(lc, "x", 1).preferred == 1.0 + 1 + len("Sync")


# --- Splitter ----------------------------------------------------------------


def _splitter():
    left, right = Checkbox("left"), Checkbox("right")
    return Splitter(left, right, fraction=0.5, min_first=3, min_second=3), left, right


def test_splitter_drag_moves_the_divider(backend):
    panel = Panel(backend)
    split, _, _ = _splitter()
    panel.add(split, x=0, y=0, w=30, h=6)
    panel.render()
    # Handle sits near x=14 (fraction 0.5 of 29 avail). Grab and drag it right.
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=14, y=2, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=22, y=2, button="left"))
    assert split._dragging is True
    assert split.fraction > 0.6


def test_splitter_drag_clamps_to_minimums(backend):
    panel = Panel(backend)
    split, _, _ = _splitter()
    panel.add(split, x=0, y=0, w=30, h=6)
    panel.render()
    # Drag the handle far past the left edge: the first pane keeps its minimum.
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=14, y=2, button="left"))
    panel.dispatch_event(Event(type=EventType.MOUSE_DRAG, x=-5, y=2, button="left"))
    panel.render()
    first, _, second = split._layout(*split._size)
    assert first.w >= split.min_first
    assert second.w >= split.min_second


def test_splitter_routes_clicks_and_focus_to_children(backend):
    panel = Panel(backend)
    split, left, right = _splitter()
    panel.add(split, x=0, y=0, w=30, h=6)
    panel.render()
    # A click on the left pane toggles its checkbox and focuses it.
    panel.dispatch_event(_click(1, 0))
    assert left.checked is True
    assert split._focused is left
    # Tab crosses into the right pane.
    panel.dispatch_event(_key("tab"))
    assert split._focused is right


def test_splitter_tab_enters_first_child(backend):
    panel = Panel(backend)
    split, left, right = _splitter()
    panel.add(split, x=0, y=0, w=30, h=6)
    # The splitter is the only focusable; focus resolves onto its first child.
    assert split._focused is left


def test_splitter_requests_resize_cursor_over_handle():
    # A pointer_shape-capable backend that records each request.
    caps = CapabilityProfile({**PROFILE_GUI_DESKTOP, "pointer_shape": True})
    backend = MemoryBackend(width=40, height=16, capabilities=caps)
    shapes = []
    backend.set_pointer_shape = lambda shape: shapes.append(shape)
    panel = Panel(backend)
    split, _, _ = _splitter()  # horizontal: side-by-side panes, vertical handle
    panel.add(split, x=0, y=0, w=30, h=6)
    panel.render()  # lays out the handle rect

    # Pointer away from the handle (over a pane): not the resize cursor.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=1.0, y=1.0))
    panel.render()
    assert shapes[-1] != "col-resize"

    # Pointer over the handle's grab zone: a left/right resize cursor.
    h = split._handle_rect
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=h.x + h.w / 2, y=2.0))
    panel.render()
    assert shapes[-1] == "col-resize"


def _cursor_backend():
    caps = CapabilityProfile({**PROFILE_GUI_DESKTOP, "pointer_shape": True})
    backend = MemoryBackend(width=40, height=16, capabilities=caps)
    shapes = []
    backend.set_pointer_shape = lambda shape: shapes.append(shape)
    return backend, shapes


def test_checkbox_requests_pointer_over_content():
    backend, shapes = _cursor_backend()
    panel = Panel(backend)
    panel.add(Checkbox("toggle me"), x=0, y=0, w=30, h=1)
    panel.render()

    # Over the mark + label: a pointing hand.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=1.0, y=0.0))
    panel.render()
    assert shapes[-1] == "pointer"

    # Past the content, in the empty slot to the right: no cursor.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=29.0, y=0.0))
    panel.render()
    assert shapes[-1] is None


# --- ComboBox ----------------------------------------------------------------


def test_combobox_opens_and_filters_then_commits(backend):
    changes = []
    panel = Panel(backend)
    combo = ComboBox(
        ["Apple", "Apricot", "Banana", "Cherry"],
        on_change=lambda s: changes.append(s),
    )
    panel.add(combo, x=0, y=0, w=22, h=1)
    panel.render()
    panel.dispatch_event(_key("down"))  # open
    assert combo.open is True
    assert len(panel._layers) == 1
    # Type "ap" -> only Apple / Apricot remain.
    panel.dispatch_event(_key("a", char="a"))
    panel.dispatch_event(_key("p", char="p"))
    assert [combo.options[i] for i in combo._filtered] == ["Apple", "Apricot"]
    panel.dispatch_event(_key("down"))   # cursor -> Apricot
    panel.dispatch_event(_key("enter"))  # commit
    assert combo.open is False
    assert combo.text == "Apricot"
    assert changes == ["Apricot"]
    assert panel._layers == []


def test_combobox_escape_keeps_text_and_closes(backend):
    panel = Panel(backend)
    combo = ComboBox(["Red", "Green"], text="Gre")
    panel.add(combo, x=0, y=0, w=22, h=1)
    panel.render()
    panel.dispatch_event(_key("down"))
    assert combo.open is True
    panel.dispatch_event(_key("escape"))
    assert combo.open is False
    assert combo.text == "Gre"  # unchanged
    assert panel._layers == []


def test_combobox_enter_accepts_custom_text_when_no_match(backend):
    changes = []
    panel = Panel(backend)
    combo = ComboBox(
        ["Red", "Green"], on_change=lambda s: changes.append(s), allow_custom=True
    )
    panel.add(combo, x=0, y=0, w=22, h=1)
    panel.render()
    panel.dispatch_event(_key("down"))
    for ch in "zzz":
        panel.dispatch_event(_key(ch, char=ch))
    assert combo._filtered == []  # nothing matches
    panel.dispatch_event(_key("enter"))
    assert combo.open is False
    assert combo.text == "zzz"
    assert changes == ["zzz"]


def test_combobox_space_types_not_commits(backend):
    panel = Panel(backend)
    combo = ComboBox(["a b", "c"], text="")
    panel.add(combo, x=0, y=0, w=22, h=1)
    panel.render()
    panel.dispatch_event(_key("down"))
    panel.dispatch_event(_key("space", char=" "))
    assert combo.open is True            # still open: space is a character
    assert combo._field.text == " "
