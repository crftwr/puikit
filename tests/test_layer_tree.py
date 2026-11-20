"""The layer tree (#233): parents, groups, hiding a subtree, removal with it."""

import pytest

from puikit import TOP_LEVEL, Event, EventType, LayerGroup, Panel
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import Label, Widget


class Recorder(Widget):
    focusable = True

    def __init__(self, name=""):
        self.name = name
        self.events = []

    def handle_event(self, event):
        self.events.append(event)
        return True

    def __repr__(self):
        return f"Recorder({self.name!r})"


def _panel():
    return Panel(MemoryBackend(width=40, height=12))


def _key(panel):
    return panel.dispatch_event(Event(type=EventType.KEY, key="enter"))


def test_without_parents_the_stack_is_flat_and_ordered_by_z():
    panel = _panel()
    a, b, c = Recorder("a"), Recorder("b"), Recorder("c")
    panel.push_layer(b, z=20)
    panel.push_layer(a, z=10)
    panel.push_layer(c, z=20)  # equal z: the later one on top
    assert panel.shown_layers() == [a, b, c]
    assert panel.top_layer() is c


def test_a_child_draws_above_its_parent_but_under_the_parents_higher_sibling():
    panel = _panel()
    viewer, other = Recorder("viewer"), Recorder("other")
    panel.push_layer(viewer, z=80)
    panel.push_layer(other, z=90)
    help_ = Recorder("help")
    panel.push_layer(help_, z=0, parent=viewer)  # z among siblings only
    assert panel.shown_layers() == [viewer, help_, other]
    assert panel.layer_parent(help_) is viewer
    assert panel.layer_children(viewer) == [help_]


def test_a_child_with_a_low_z_is_still_above_its_parent():
    panel = _panel()
    terminal = Recorder("terminal")
    panel.push_layer(terminal, z=75)
    confirm = Recorder("confirm")
    panel.push_layer(confirm, z=1, parent=terminal)
    assert panel.top_layer() is confirm
    assert _key(panel) and confirm.events and not terminal.events


def test_hiding_a_group_hides_its_subtree_and_keeps_it():
    panel = _panel()
    files = panel.push_layer_group(z=0, name="files")
    term = panel.push_layer_group(z=10, name="term")
    assert isinstance(files, LayerGroup)
    viewer = Recorder("viewer")
    panel.push_layer(viewer, z=80, parent=files)
    tv = Recorder("terminal")
    panel.push_layer(tv, z=1, parent=term)
    panel.set_layer_hidden(term, True)
    assert panel.shown_layers() == [viewer]
    assert _key(panel) and viewer.events and not tv.events

    panel.set_layer_hidden(term, False)
    panel.set_layer_hidden(files, True)
    assert panel.shown_layers() == [tv]
    assert panel.is_layer_shown(tv) and not panel.is_layer_shown(viewer)
    assert panel.has_layer(viewer)  # still there, hidden
    assert panel.shown_layers(under=files) == []
    assert panel.top_layer(under=term) is tv


def test_groups_are_not_drawn_or_modal():
    panel = _panel()
    group = panel.push_layer_group(z=5)
    assert panel.shown_layers() == []
    assert not panel.has_layers
    base = Recorder("base")
    panel.add(base, x=0, y=0, w=40, h=12)
    assert _key(panel) and base.events
    panel.render()  # a group node draws nothing and does not fail
    assert panel.layer_children() == [group]


def test_removing_a_parent_removes_its_children():
    panel = _panel()
    viewer, dialog, popup = Recorder("v"), Recorder("d"), Recorder("p")
    panel.push_layer(viewer, z=80)
    panel.push_layer(dialog, z=1, parent=viewer)
    panel.push_layer(popup, z=1, parent=dialog)
    assert panel.remove_layer(viewer)
    assert panel.shown_layers() == []
    assert not panel.has_layer(dialog) and not panel.has_layer(popup)


def test_a_hidden_layer_can_remove_itself():
    panel = _panel()
    group = panel.push_layer_group(z=0)
    progress = Recorder("progress")
    panel.push_layer(progress, z=70, parent=group)
    panel.set_layer_hidden(group, True)
    assert panel.remove_layer(progress)  # it closed itself while hidden
    panel.set_layer_hidden(group, False)
    assert panel.shown_layers() == []


def test_pop_layer_takes_the_topmost_shown_with_its_subtree():
    panel = _panel()
    a, b = Recorder("a"), Recorder("b")
    panel.push_layer(a, z=10)
    panel.push_layer(b, z=1, parent=a)
    assert panel.pop_layer() is b
    assert panel.shown_layers() == [a]


def test_default_parent_takes_layers_pushed_without_one():
    panel = _panel()
    files = panel.push_layer_group(z=0)
    term = panel.push_layer_group(z=10)
    panel.set_default_layer_parent(files)
    dlg = Recorder("dialog")
    panel.push_layer(dlg, z=70)  # code that knows nothing of groups
    assert panel.layer_parent(dlg) is files
    app_wide = Recorder("quit")
    panel.push_layer(app_wide, z=200, parent=TOP_LEVEL)
    assert panel.layer_parent(app_wide) is None
    panel.set_layer_hidden(files, True)
    assert panel.shown_layers() == [app_wide]
    panel.set_default_layer_parent(term)
    assert panel.default_layer_parent is term
    # Removing the default parent forgets it.
    panel.remove_layer(term)
    assert panel.default_layer_parent is None


def test_unknown_parent_is_refused():
    panel = _panel()
    with pytest.raises(ValueError):
        panel.push_layer(Recorder(), parent=Recorder())
    with pytest.raises(ValueError):
        panel.set_layer_hidden(Recorder(), True)


def test_hidden_layers_do_not_draw():
    backend = MemoryBackend(width=20, height=3)
    panel = Panel(backend)
    group = panel.push_layer_group()
    panel.push_layer(Label("SHOWN"), z=1, parent=group, hints={"x": 0, "y": 0, "w": 5, "h": 1})
    panel.render()
    assert backend.snapshot()[0].startswith("SHOWN")
    panel.set_layer_hidden(group, True)
    panel.render()
    assert not backend.snapshot()[0].startswith("SHOWN")
