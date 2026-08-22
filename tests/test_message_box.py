"""Tests for the MessageBox modal, run against TUI and GUI memory profiles."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI
from puikit.widgets import Label, show_message_box
from puikit.backends.memory_backend import MemoryBackend


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=60, height=20, capabilities=request.param)


def _key(name, char=None):
    return Event(type=EventType.KEY, key=name, char=char)


def test_message_box_pushes_modal_layer_and_renders(backend):
    panel = Panel(backend)
    show_message_box(panel, "Something happened.", title="Notice", buttons=("OK",))
    assert len(panel._layers) == 1
    panel.render()
    assert any("Notice" in row for row in backend.snapshot())
    assert any("Something happened." in row for row in backend.snapshot())
    assert any("OK" in row for row in backend.snapshot())


def test_message_box_enter_returns_focused_button(backend):
    results = []
    panel = Panel(backend)
    show_message_box(
        panel, "Save changes?", title="Confirm",
        buttons=("Save", "Discard", "Cancel"), default=0, on_result=results.append,
    )
    panel.render()
    panel.dispatch_event(_key("enter"))
    assert results == ["Save"]
    assert panel._layers == []


def test_message_box_arrows_move_focus(backend):
    results = []
    panel = Panel(backend)
    show_message_box(
        panel, "Pick", buttons=("A", "B", "C"), on_result=results.append,
    )
    panel.render()
    panel.dispatch_event(_key("right"))
    panel.dispatch_event(_key("right"))
    panel.dispatch_event(_key("enter"))
    assert results == ["C"]


def test_message_box_escape_picks_cancel(backend):
    results = []
    panel = Panel(backend)
    show_message_box(
        panel, "Quit?", buttons=("Yes", "No"), on_result=results.append,
    )
    panel.render()
    panel.dispatch_event(_key("escape"))  # cancel defaults to the last button
    assert results == ["No"]
    assert panel._layers == []


def test_message_box_focus_brackets_symmetric_on_tui():
    # An odd-width box (sized to its message) centering an even-width button row
    # gives the row a half-unit origin; a whole-unit backend rounds each draw
    # coordinate independently, so without snapping the origin the focus bracket
    # "[ OK ]" desyncs from its label into "[ OK]". The row origin is snapped to
    # the base unit grid so the brackets stay equidistant from the label.
    backend = MemoryBackend(width=60, height=20, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    show_message_box(panel, "Your changes have been saved.", title="Saved")
    panel.render()
    row = next(r for r in backend.snapshot() if "[" in r and "]" in r)
    open_i, close_i = row.index("["), row.index("]")
    label = row[open_i + 1 : close_i].strip()
    assert label == "OK"
    # Equal padding on each side of the centered label.
    assert open_i + 1 != close_i  # there is room between the brackets
    left_pad = row[open_i + 1 :].index(label[0])
    right_pad = close_i - (open_i + 1 + left_pad + len(label))
    assert left_pad == right_pad


def test_message_box_click_activates_button(backend):
    results = []
    panel = Panel(backend)
    box = show_message_box(
        panel, "Choose", buttons=("Left", "Right"), on_result=results.append,
    )
    panel.render()
    rect = panel._layers[0].rect
    # box-local rect of the "Right" button, captured during draw
    x0, _x1, y0, _y1 = box._button_x[1]
    # Click that button in screen coords; the modal layer gets it translated.
    panel.dispatch_event(
        Event(type=EventType.MOUSE_CLICK, x=rect.x + x0, y=rect.y + y0, button="left")
    )
    assert results == ["Right"]
    assert panel._layers == []


def test_message_box_markdown_link_click_opens_url(backend):
    # A click on the message region routes to the MarkdownView, which opens the
    # link under it — so a URL in a markdown message box is really clickable.
    panel = Panel(backend)
    opened = []
    panel.open_url = opened.append
    box = show_message_box(
        panel, "Project home:\n\nhttps://example.com/repo", markdown=True,
    )
    panel.render()
    rect = panel._layers[0].rect
    # view-local link span captured during draw; aim at its center, translated
    # out through the view's box-local origin to screen coords.
    x0, y0, x1, y1, url = box._md._link_hits[0]
    assert url == "https://example.com/repo"
    mx0, my0, _mx1, _my1 = box._md_rect
    panel.dispatch_event(Event(
        type=EventType.MOUSE_CLICK,
        x=rect.x + mx0 + (x0 + x1) / 2, y=rect.y + my0 + (y0 + y1) / 2,
        button="left",
    ))
    assert opened == ["https://example.com/repo"]
    assert len(panel._layers) == 1  # the box stays up; only a button closes it


def test_message_box_markdown_hard_break_sizes_to_longest_row():
    # A hard line break (trailing backslash) starts a fresh row, so the box must
    # size its width to the paragraph's longest row — the same as the long line
    # alone — not to all the rows laid end to end.
    long_line = "The widest row in the message by a comfortable margin"
    widths = []
    for message in (f"{long_line}\\\nshort", long_line):
        backend = MemoryBackend(width=80, height=24, capabilities=PROFILE_TUI)
        panel = Panel(backend)
        show_message_box(panel, message, markdown=True)
        panel.render()
        widths.append(panel._layers[0].rect.w)
    assert widths[0] == widths[1]


def test_message_box_close_removes_itself_not_the_topmost_layer(backend):
    # A layer pushed above the box after it opened must survive the box
    # closing: the box removes its own layer by identity rather than popping
    # whatever happens to be on top (xefm#333's bug family).
    results = []
    panel = Panel(backend)
    box = show_message_box(panel, "Sure?", buttons=("OK",), on_result=results.append)
    cover = Label("busy")
    panel.push_layer(cover, z=99, hints={"w": 10, "h": 2})
    box._close(0)
    assert results == ["OK"]
    assert [slot.widget for slot in panel._layers] == [cover]
