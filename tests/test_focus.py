"""Cross-boundary keyboard focus traversal (puikit.focus).

Tab / Shift+Tab walk the whole widget tree from the Panel root: they descend
into containers, cross from one pane to the next instead of trapping inside one,
and wrap only at the root. Run against TUI and GUI profiles alike so the one
mechanism is verified on every backend.
"""

import pytest

from puikit import (
    Event,
    EventType,
    Panel,
    PROFILE_GUI_DESKTOP,
    PROFILE_TUI,
    TextAttribute,
)
from puikit.layout import Item, VSplit
from puikit.widgets import (
    Checkbox,
    Container,
    Label,
    LayoutView,
    ListView,
    ScrollView,
    Tabs,
)


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    from puikit.backends.memory_backend import MemoryBackend

    return MemoryBackend(width=30, height=12, capabilities=request.param)


def _tab(shift=False):
    mods = frozenset({"shift"}) if shift else frozenset()
    return Event(type=EventType.KEY, key="tab", modifiers=mods)


def _scroller(*controls):
    items = [(Label("head"), 1)] + [(c, 1) for c in controls]
    return ScrollView(items, gap=1)


def test_tab_crosses_pane_boundary_instead_of_trapping(backend):
    # Two panes, each a ScrollView with two checkboxes. Tab must not wrap inside
    # the first pane; once it runs off that pane's end it moves into the next.
    a1, a2, b1, b2 = (Checkbox(n) for n in ("a1", "a2", "b1", "b2"))
    pane_a, pane_b = _scroller(a1, a2), _scroller(b1, b2)
    panel = Panel(backend)
    panel.add(pane_a, x=0, y=0, w=14, h=12)
    panel.add(pane_b, x=15, y=0, w=14, h=12)

    assert pane_a._focused is a1            # first focusable holds focus
    panel.dispatch_event(_tab())
    assert pane_a._focused is a2
    panel.dispatch_event(_tab())            # off pane_a's end -> into pane_b
    assert panel.focused is pane_b
    assert pane_b._focused is b1
    panel.dispatch_event(_tab())
    assert pane_b._focused is b2


def test_tab_wraps_only_at_the_root(backend):
    a1, b1 = Checkbox("a1"), Checkbox("b1")
    pane_a, pane_b = _scroller(a1), _scroller(b1)
    panel = Panel(backend)
    panel.add(pane_a, x=0, y=0, w=14, h=12)
    panel.add(pane_b, x=15, y=0, w=14, h=12)

    panel.dispatch_event(_tab())            # a1 -> off pane_a -> b1
    assert panel.focused is pane_b and pane_b._focused is b1
    panel.dispatch_event(_tab())            # off the last pane -> wrap to a1
    assert panel.focused is pane_a and pane_a._focused is a1


def test_shift_tab_reverses_across_the_boundary(backend):
    a1, a2, b1 = Checkbox("a1"), Checkbox("a2"), Checkbox("b1")
    pane_a, pane_b = _scroller(a1, a2), _scroller(b1)
    panel = Panel(backend)
    panel.add(pane_a, x=0, y=0, w=14, h=12)
    panel.add(pane_b, x=15, y=0, w=14, h=12)

    panel.focus(pane_b)                     # focus the second pane's only child
    panel.dispatch_event(_tab(shift=True))  # backward -> last child of pane_a
    assert panel.focused is pane_a and pane_a._focused is a2


def test_container_pane_participates_in_traversal(backend):
    # A plain Container (not a ScrollView) must traverse too, and its end must
    # release focus to the next pane.
    a1, a2, b1 = Checkbox("a1"), Checkbox("a2"), Checkbox("b1")
    cont = Container()
    cont.add(a1, x=0, y=0, w=10, h=1)
    cont.add(a2, x=0, y=1, w=10, h=1)
    panel = Panel(backend)
    panel.add(cont, x=0, y=0, w=12, h=6)
    panel.add(_scroller(b1), x=13, y=0, w=14, h=6)

    panel.dispatch_event(_tab())
    assert cont._focused is a2
    panel.dispatch_event(_tab())            # off the container -> next pane
    assert panel.focused is not cont
    assert panel.focused._focused is b1


def test_tab_descends_into_active_tab_content(backend):
    # Tabs is a container: Tab descends into the active tab's content and steps
    # through it; switching tabs moves focus to the new content.
    a1, a2, other = Checkbox("a1"), Checkbox("a2"), Checkbox("other")
    body = _scroller(a1, a2)
    tabs = Tabs([("first", body), ("second", other)])
    panel = Panel(backend)
    panel.add(tabs, x=0, y=0, w=20, h=10)

    assert tabs.get_focused() is body
    assert body._focused is a1
    panel.dispatch_event(_tab())            # steps within the active content
    assert body._focused is a2


def test_focusable_scrollview_without_controls_is_a_stop(backend):
    # A scrollable pane with no focusable children is still a focus stop, so the
    # keyboard can reach and scroll it; a following control comes after it.
    text_only = ScrollView([(Label(f"line {i}"), 1) for i in range(20)])
    after = Checkbox("after")
    panel = Panel(backend)
    panel.add(text_only, x=0, y=0, w=14, h=4)
    panel.add(_scroller(after), x=15, y=0, w=14, h=8)

    assert panel.focused is text_only       # lands on the scrollable view
    panel.dispatch_event(_tab())            # no children to step -> next pane
    assert panel.focused is not text_only
    assert panel.focused._focused is after


def test_click_moves_focus_across_panes(backend):
    a1, b1 = Checkbox("a1"), Checkbox("b1")
    pane_a, pane_b = _scroller(a1), _scroller(b1)
    panel = Panel(backend)
    panel.add(pane_a, x=0, y=0, w=14, h=12)
    panel.add(pane_b, x=15, y=0, w=14, h=12)
    panel.render()

    assert panel.focused is pane_a
    # b1 sits in pane_b at content y=2 (head row 0, gap row 1); pane_b is at x=15.
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=16, y=2, button="left"))
    assert panel.focused is pane_b
    assert b1.checked is True


def test_click_focuses_nested_child_on_press_not_release(backend):
    # Clicking a control inside a container must move focus into it on the press
    # (MOUSE_DOWN), not wait for the release — so the focus cue is immediate.
    a1, b1 = Checkbox("a1"), Checkbox("b1")
    pane_a, pane_b = _scroller(a1), _scroller(b1)
    panel = Panel(backend)
    panel.add(pane_a, x=0, y=0, w=14, h=12)
    panel.add(pane_b, x=15, y=0, w=14, h=12)
    panel.render()

    assert panel.focused is pane_a and pane_a._focused is a1
    # b1 sits in pane_b (head row 0, gap row 1) at content y=2; pane_b is at x=15.
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=16, y=2, button="left"))
    # Focus has descended all the way to b1 on the press, before any release.
    assert panel.focused is pane_b
    assert pane_b._focused is b1


def test_event_translated_keeps_subunit_precision():
    # Routing must not quantize coordinates to whole cells, or a fractional edge
    # hit-tests differently than the geometric hover/press cue.
    e = Event(type=EventType.MOUSE_DOWN, x=5.7, y=2.5, button="left")
    t = e.translated(-1.0, -0.4)
    assert (t.x, t.y) == pytest.approx((4.7, 2.1))


def test_click_top_edge_routes_to_widget_at_fractional_boundary(backend):
    # A child whose top edge sits at a fractional base unit (pixel layout) must
    # receive a press at that exact edge, so focus lands on it — not on the row
    # above — matching where the geometric press cue lights.
    top, bottom = Checkbox("top"), Checkbox("bottom")
    cont = Container()
    cont.add(top, x=0, y=0, w=6, h=1.5)
    cont.add(bottom, x=0, y=1.5, w=6, h=1.5)
    panel = Panel(backend)
    panel.add(cont, x=0, y=0, w=6, h=3)
    panel.render()
    # Press exactly on the bottom child's top edge (y = 1.5).
    panel.dispatch_event(Event(type=EventType.MOUSE_DOWN, x=1.0, y=1.5, button="left"))
    assert cont.get_focused() is bottom


def test_list_selection_is_a_strong_cue_only_while_focused(backend):
    # A list's selected row reads as active (reverse video) while the list holds
    # focus, and dims to the muted selection background when focus is elsewhere
    # — the same focus-aware cue every control draws.
    items = ListView(["alpha", "beta", "gamma"])
    other = Checkbox("other")
    panel = Panel(backend)
    panel.add(items, x=0, y=0, w=12, h=4)
    panel.add(other, x=14, y=0, w=10, h=1)

    panel.focus(items)
    panel.render()
    assert backend.style_at(0, 0).attr & TextAttribute.REVERSE   # active highlight

    panel.focus(other)
    panel.render()
    sel = backend.style_at(0, 0)
    assert not sel.attr & TextAttribute.REVERSE                  # dimmed, not active
    assert sel.bg == panel.theme.selection_inactive_bg          # muted, focus elsewhere


def test_tab_descends_through_layoutview_host(backend):
    # Mirrors the demo_catalog layout: a nav list beside a LayoutView page host
    # whose hosted layout contains a ScrollView of controls. Tab must walk from
    # the nav, into the page host, down through the ScrollView, onto each
    # control — not stop on the page as one opaque stop.
    nav = ListView(["page a", "page b"])
    c1, c2 = Checkbox("c1"), Checkbox("c2")
    page = LayoutView(VSplit(
        Item(Label("heading"), size=1),
        Item(_scroller(c1, c2), weight=1),
    ))
    panel = Panel(backend)
    panel.add(nav, x=0, y=0, w=12, h=12)
    panel.add(page, x=13, y=0, w=16, h=12)
    panel.render()  # populates the host's placements

    assert panel.focused is nav
    panel.dispatch_event(_tab())            # nav -> into the page's first control
    assert panel.focused is page
    inner = page._focused._focused          # ScrollView's focused child
    assert inner is c1
    panel.dispatch_event(_tab())
    assert page._focused._focused is c2
    panel.dispatch_event(_tab())            # off the page's end -> wrap to nav
    assert panel.focused is nav


def test_inert_page_host_is_skipped_so_focus_stays_visible(backend):
    # The Layout demo page has no focusable controls — only static widgets. Its
    # LayoutView host must NOT become a focus stop (it draws no cue), so Tab
    # keeps focus on the nav, where it is visible, instead of parking on the
    # invisible host.
    nav = ListView(["page a", "page b"])
    page = LayoutView(VSplit(
        Item(Label("just a caption"), size=1),
        Item(Label("no controls here"), weight=1),
    ))
    panel = Panel(backend)
    panel.add(nav, x=0, y=0, w=12, h=12)
    panel.add(page, x=13, y=0, w=16, h=12)
    panel.render()

    assert panel.focused is nav
    panel.dispatch_event(_tab())            # nothing focusable past the nav
    assert panel.focused is nav             # focus stays where it is visible


def test_space_activates_list_selection(backend):
    fired = []
    items = ListView(["alpha", "beta"], on_select=lambda i, s: fired.append(s))
    panel = Panel(backend)
    panel.add(items, x=0, y=0, w=12, h=4)
    panel.dispatch_event(Event(type=EventType.KEY, key="space"))
    assert fired == ["alpha"]
