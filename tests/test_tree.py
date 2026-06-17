"""Tests for the TreeView widget, run against TUI and GUI memory profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.widgets import TreeNode, TreeView
from puikit.backends.memory_backend import MemoryBackend


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=30, height=10, capabilities=request.param)


def _key(name):
    return Event(type=EventType.KEY, key=name)


def _tree():
    return [
        TreeNode(
            "src",
            children=[TreeNode("main.py"), TreeNode("util.py")],
            expanded=True,
        ),
        TreeNode("docs", children=[TreeNode("guide.md")]),  # collapsed
        TreeNode("README"),
    ]


def test_tree_renders_expanded_and_collapsed_markers(backend):
    panel = Panel(backend)
    panel.add(TreeView(_tree()), x=0, y=0, w=30, h=10)
    panel.render()
    snap = backend.snapshot()
    assert snap[0].startswith("▾ src")          # expanded branch
    assert "main.py" in snap[1] and "util.py" in snap[2]  # indented children
    assert any(row.startswith("▸ docs") for row in snap)  # collapsed branch


def test_tree_right_expands_left_collapses(backend):
    tree = _tree()
    docs = tree[1]
    panel = Panel(backend)
    view = TreeView(tree)
    panel.add(view, x=0, y=0, w=30, h=10)
    panel.render()
    # Move selection down to "docs" (rows: src, main.py, util.py, docs=3).
    for _ in range(3):
        panel.dispatch_event(_key("down"))
    assert view.selected == 3
    panel.dispatch_event(_key("right"))  # expand docs
    assert docs.expanded is True
    panel.dispatch_event(_key("left"))   # collapse docs
    assert docs.expanded is False


def test_tree_left_on_child_moves_to_parent(backend):
    panel = Panel(backend)
    view = TreeView(_tree())
    panel.add(view, x=0, y=0, w=30, h=10)
    panel.render()
    panel.dispatch_event(_key("down"))   # -> main.py (child of src)
    assert view.selected == 1
    panel.dispatch_event(_key("left"))   # leaf: move to parent "src"
    assert view.selected == 0


def test_tree_enter_activates_and_toggles_branch(backend):
    activated = []
    tree = _tree()
    panel = Panel(backend)
    view = TreeView(tree, on_activate=activated.append)
    panel.add(view, x=0, y=0, w=30, h=10)
    panel.render()
    panel.dispatch_event(_key("enter"))  # on "src" (expanded) -> collapse + activate
    assert tree[0].expanded is False
    assert activated == [tree[0]]


def test_tree_click_expander_toggles(backend):
    tree = _tree()
    panel = Panel(backend)
    view = TreeView(tree)
    panel.add(view, x=0, y=0, w=30, h=10)
    panel.render()
    # Click the expander marker (column 0) on row 0 ("src") collapses it.
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=0, y=0, button="left"))
    assert tree[0].expanded is False


def test_tree_on_select_fires_when_selection_moves(backend):
    selected = []
    panel = Panel(backend)
    view = TreeView(_tree(), on_select=lambda n: selected.append(n.label))
    panel.add(view, x=0, y=0, w=30, h=10)
    panel.render()
    panel.dispatch_event(_key("down"))
    assert selected == ["main.py"]
