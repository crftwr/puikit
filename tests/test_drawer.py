"""Tests for the Drawer edge panel, run against TUI and GUI memory profiles."""

import pytest

from puikit import CapabilityProfile, Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.widgets import Button, Container, Label, show_drawer
from puikit.widgets.drawer import ROUNDED_CORNERS
from puikit.backends.memory_backend import MemoryBackend


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=60, height=20, capabilities=request.param)


class _VectorBackend(MemoryBackend):
    """A grid backend that *claims* vector_shapes so the rounded-face path can
    be exercised headlessly (the real MemoryBackend masks it off)."""

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile({**self._capabilities, "vector_shapes": True})


def _key(name, modifiers=frozenset()):
    return Event(type=EventType.KEY, key=name, modifiers=modifiers)


def _click(x, y):
    return Event(type=EventType.MOUSE_CLICK, x=x, y=y, button="left")


def test_show_drawer_pushes_layer_and_renders_title(backend):
    panel = Panel(backend)
    show_drawer(panel, Label("Drawer body"), side="left", title="Filters")
    assert len(panel._layers) == 1
    panel.render()
    rows = backend.snapshot()
    assert any("Filters" in row for row in rows)
    assert any("Drawer body" in row for row in rows)


@pytest.mark.parametrize(
    "side,check",
    [
        ("left", lambda r, sw, sh: r.x == 0 and r.h == sh),
        ("right", lambda r, sw, sh: r.x + r.w == sw and r.h == sh),
        ("top", lambda r, sw, sh: r.y == 0 and r.w == sw),
        ("bottom", lambda r, sw, sh: r.y + r.h == sh and r.w == sw),
    ],
)
def test_drawer_is_anchored_to_its_edge(backend, side, check):
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side=side)
    rect = panel._layers[0].rect
    sw, sh = backend.size_units
    assert check(rect, sw, sh)


def test_drawer_fills_cross_axis(backend):
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side="left", size=18)
    rect = panel._layers[0].rect
    sw, sh = backend.size_units
    assert rect.w == 18
    assert rect.h == sh


@pytest.mark.parametrize(
    "side,check",
    [
        ("left", lambda r, sw, sh: r.x == 0 and r.h == sh),
        ("right", lambda r, sw, sh: r.x + r.w == sw and r.h == sh),
        ("top", lambda r, sw, sh: r.y == 0 and r.w == sw),
        ("bottom", lambda r, sw, sh: r.y + r.h == sh and r.w == sw),
    ],
)
def test_drawer_reflows_on_window_resize(backend, side, check):
    # The drawer's geometry is derived from the window size, so a resize must
    # re-anchor it to its edge and re-fill the cross-axis on the next render —
    # not leave it frozen at the size it was opened with (issue #59).
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side=side)
    panel.render()
    backend._width, backend._height = 84, 32
    panel.render()
    rect = panel._layers[0].rect
    sw, sh = backend.size_units
    assert (sw, sh) == (84, 32)
    assert check(rect, sw, sh)


def test_escape_closes_drawer(backend):
    closed = []
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side="right", on_close=lambda: closed.append(True))
    panel.render()
    panel.dispatch_event(_key("escape"))
    assert panel._layers == []
    assert closed == [True]


def test_scrim_click_closes_modal_drawer(backend):
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side="left", size=20)
    panel.render()
    # Click well to the right of a 20-wide left drawer: on the dimmed scrim.
    panel.dispatch_event(_click(50, 10))
    assert panel._layers == []


def test_scrim_click_keeps_non_modal_drawer(backend):
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side="left", size=20, modal=False)
    panel.render()
    panel.dispatch_event(_click(50, 10))
    assert len(panel._layers) == 1


def test_click_inside_drawer_reaches_content(backend):
    clicks = []
    button = Button("Go", on_click=lambda: clicks.append(True))
    content = Container()
    content.add(button, x=0, y=0, w=8, h=1)
    panel = Panel(backend)
    show_drawer(panel, content, side="left", size=24)
    panel.render()
    rect = panel._layers[0].rect
    # The content starts one pad in from the drawer origin; the button is its
    # first row. Click inside the drawer, not on the scrim.
    panel.dispatch_event(_click(rect.x + 2, rect.y + 1))
    assert clicks == [True]
    assert len(panel._layers) == 1  # an inside click never dismisses the drawer


def test_tab_cycles_focus_within_content(backend):
    first = Button("A", on_click=lambda: None)
    second = Button("B", on_click=lambda: None)
    content = Container()
    content.add(first, x=0, y=0, w=8, h=1)
    content.add(second, x=0, y=2, w=8, h=1)
    panel = Panel(backend)
    drawer = show_drawer(panel, content, side="bottom")
    panel.render()
    assert content.get_focused() is first
    panel.dispatch_event(_key("tab"))
    assert content.get_focused() is second
    # Wrapping: the drawer is the modal focus root, so it cycles back.
    panel.dispatch_event(_key("tab"))
    assert content.get_focused() is first


def test_invalid_side_rejected(backend):
    panel = Panel(backend)
    with pytest.raises(ValueError):
        show_drawer(panel, Label("x"), side="middle")


# --- rounded face (vector backends) --------------------------------------------


@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_vector_drawer_rounds_inner_corners(side):
    # On a vector backend the drawer paints a rounded face whose rounded corners
    # are the inner ones (facing the page); the edge-flush corners stay square.
    backend = _VectorBackend(width=60, height=20, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side=side, radius=10)
    panel.render()
    assert backend.round_rect_calls, "drawer painted no rounded face"
    *_, radius, style, hints = backend.round_rect_calls[0]
    assert radius == 10
    assert hints.get("corners") == ROUNDED_CORNERS[side]
    assert hints.get("fill") is True


@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_shadow_silhouette_matches_rounded_corners(side):
    # The drop shadow is cast with the same radius/corners as the face, so it
    # follows the rounded outline instead of a square rect.
    backend = _VectorBackend(width=60, height=20, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side=side, radius=10)
    panel.render()
    assert backend.shadow_calls, "no shadow drawn"
    *_, radius, corners = backend.shadow_calls[0]
    assert radius == 10
    assert corners == ROUNDED_CORNERS[side]


def test_tui_drawer_has_flat_fill_and_no_shadow():
    # A character grid cannot round corners: the round_rect fallback fills the
    # rect flat (no recorded vector call) and there is no shadow capability.
    backend = MemoryBackend(width=60, height=20, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    show_drawer(panel, Label("x"), side="left")
    panel.render()
    assert backend.round_rect_calls == []
    assert backend.shadow_calls == []
