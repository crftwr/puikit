"""Tests for the JsonView widget, run against TUI and GUI memory profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import JsonView


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=40, height=14, capabilities=request.param)


def _key(name, mods=frozenset()):
    return Event(type=EventType.KEY, key=name, modifiers=mods)


def _data():
    return {
        "name": "tfm",
        "tags": ["tui", "files"],
        "nested": {"ok": True, "count": 42, "z": None},
    }


def test_top_level_keys_render_with_markers(backend):
    panel = Panel(backend)
    panel.add(JsonView(_data()), x=0, y=0, w=40, h=14)
    panel.render()
    snap = backend.snapshot()
    assert snap[0].startswith('  name: "tfm"')      # scalar leaf, no expander
    assert any(r.startswith("▸ tags: [2]") for r in snap)     # collapsed array
    assert any(r.startswith("▸ nested: {3}") for r in snap)   # collapsed object


def test_right_expands_left_collapses(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_key("down"))     # select "tags"
    panel.dispatch_event(_key("right"))    # expand it
    panel.render()
    snap = backend.snapshot()
    assert view.roots[1].expanded is True
    assert any('0: "tui"' in r for r in snap) and any('1: "files"' in r for r in snap)
    panel.dispatch_event(_key("left"))     # collapse
    assert view.roots[1].expanded is False


def test_scalar_document_renders_single_leaf(backend):
    panel = Panel(backend)
    panel.add(JsonView(42), x=0, y=0, w=40, h=14)
    panel.render()
    assert backend.snapshot()[0].startswith("  42")


def test_search_expands_ancestors_and_reports_status(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    view.search_begin()
    count = view.search_set("count")       # inside the collapsed "nested"
    assert count == 1
    assert view.roots[2].expanded is True  # ancestor auto-expanded
    assert view.search_status() == (1, 1)
    panel.render()
    assert any("count: 42" in r for r in backend.snapshot())


def test_search_navigate_wraps(backend):
    view = JsonView({"a": "hit one", "b": {"c": "hit two"}})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    view.search_begin()
    assert view.search_set("hit") == 2
    assert view.search_status() == (1, 2)
    view.search_navigate(1)
    assert view.search_status() == (2, 2)
    view.search_navigate(1)                # wrap back to the first
    assert view.search_status() == (1, 2)


def test_search_cancel_restores_scroll(backend):
    view = JsonView({str(i): i for i in range(40)})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    view.offset = 6.0
    view.search_begin()
    view.search_set("39")                  # scrolls away to the match
    view.search_cancel()
    assert view.offset == 6.0              # restored


def test_ctrl_c_copies_selected_value(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_key("c", frozenset({"ctrl"})))   # selected = "name"
    assert backend.get_clipboard() == '"tfm"'
    panel.dispatch_event(_key("down"))                     # select "tags"
    panel.dispatch_event(_key("c", frozenset({"cmd"})))
    assert backend.get_clipboard() == '["tui", "files"]'
