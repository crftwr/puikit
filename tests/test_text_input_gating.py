"""Focus-gated text input: the Panel engages the backend's text-input/IME
system only while a widget that wants text (TextEdit/ComboBox) holds focus, and
delivers plain command keys otherwise. See docs / the keyboard contract."""

from puikit import Panel
from puikit.backends.memory_backend import MemoryBackend
from puikit.focus import FocusContainer
from puikit.widgets import TextEdit
from puikit.widgets.base import Widget


class _Focusable(Widget):
    focusable = True  # a non-text focus stop (stands in for a file list, button)


def test_text_widget_focus_engages_and_releases_input():
    backend = MemoryBackend()
    panel = Panel(backend)
    nav = _Focusable()
    field = TextEdit()
    panel.add(nav, 0, 0, 10, 3)
    panel.add(field, 0, 4, 10, 1)

    # Non-text widget focused: input stays disengaged.
    panel.set_focused(nav)
    panel.render()
    assert backend.text_input_active is False
    assert backend.text_input_calls == []

    # Focus a text field: begin_text_input fires once.
    panel.set_focused(field)
    panel.render()
    assert backend.text_input_active is True
    assert backend.text_input_calls == ["begin"]

    # Re-render without a focus change must not re-toggle (idempotent).
    panel.render()
    assert backend.text_input_calls == ["begin"]

    # Focus back to the non-text widget: end_text_input fires.
    panel.set_focused(nav)
    panel.render()
    assert backend.text_input_active is False
    assert backend.text_input_calls == ["begin", "end"]


def test_focused_leaf_descends_into_containers():
    # A TextEdit nested inside a focus container must still engage input — the
    # Panel resolves focus down to the leaf, not just the top-level slot.
    backend = MemoryBackend()
    panel = Panel(backend)
    field = TextEdit()

    class _Host(Widget, FocusContainer):
        focusable = True

        def __init__(self, child):
            self._child = child
            self._focused = child

        def focus_children(self):
            return [self._child]

    host = _Host(field)
    panel.add(host, 0, 0, 10, 3)
    panel.set_focused(host)
    panel.render()
    assert panel.focused_leaf() is field
    assert backend.text_input_active is True


def test_no_focus_keeps_input_disengaged():
    backend = MemoryBackend()
    panel = Panel(backend)
    panel.add(_Focusable(), 0, 0, 10, 3)
    panel.render()
    assert backend.text_input_active is False
    assert backend.text_input_calls == []
