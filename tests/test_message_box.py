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


def test_a_question_that_must_be_answered_ignores_escape(backend):
    results = []
    panel = Panel(backend)
    box = show_message_box(
        panel, "Which?", buttons=("A", "B"), on_result=results.append,
        dismissable=False,
    )
    panel.render()
    panel.dispatch_event(_key("escape"))
    assert results == []
    assert [slot.widget for slot in panel._layers] == [box]
    panel.dispatch_event(_key("enter"))
    assert results == ["A"]
    assert panel._layers == []


@pytest.mark.parametrize("stray", [_key("enter"), _key("space", " "), _key("y", "y")],
                         ids=["enter", "space", "letter"])
def test_with_no_default_a_stray_key_presses_nothing(backend, stray):
    results = []
    panel = Panel(backend)
    box = show_message_box(
        panel, "Allow?", buttons=("Allow", "Always", "Deny"), default=None,
        on_result=results.append,
    )
    panel.render()
    assert box.default is None and box.focused is None
    panel.dispatch_event(stray)
    panel.render()
    assert results == [] and box.focused is None
    assert [slot.widget for slot in panel._layers] == [box]


@pytest.mark.parametrize("keys, chosen", [
    (["left"], "Allow"),
    (["up"], "Allow"),
    (["right"], "Deny"),
    (["down"], "Deny"),
    (["tab"], "Deny"),
    (["left", "right"], "Always"),
    (["right", "left"], "Always"),
])
def test_with_no_default_the_first_arrow_takes_the_end_it_points_to(backend, keys, chosen):
    results = []
    panel = Panel(backend)
    show_message_box(
        panel, "Allow?", buttons=("Allow", "Always", "Deny"), default=None,
        on_result=results.append,
    )
    panel.render()
    for key in keys:
        panel.dispatch_event(_key(key))
        panel.render()
    panel.dispatch_event(_key("enter"))
    assert results == [chosen]
    assert panel._layers == []


@pytest.mark.parametrize("act, chosen", [("escape", "No"), ("click", "Yes")])
def test_with_no_default_escape_and_a_click_work_at_once(backend, act, chosen):
    results = []
    panel = Panel(backend)
    box = show_message_box(
        panel, "Go?", buttons=("Yes", "No"), default=None, on_result=results.append,
    )
    panel.render()
    if act == "escape":
        panel.dispatch_event(_key("escape"))
    else:
        rect = panel._layers[0].rect
        x0, _x1, y0, _y1 = box._button_x[0]
        panel.dispatch_event(Event(
            type=EventType.MOUSE_CLICK, x=rect.x + x0, y=rect.y + y0, button="left",
        ))
    assert results == [chosen]
    assert panel._layers == []


def test_with_no_default_no_button_is_marked():
    backend = MemoryBackend(width=60, height=20, capabilities=PROFILE_TUI)
    panel = Panel(backend)
    show_message_box(panel, "Go?", buttons=("Yes", "No"), default=None)
    panel.render()
    assert not any("[" in row for row in backend.snapshot())
    panel.dispatch_event(_key("right"))
    panel.render()
    row = next(r for r in backend.snapshot() if "[" in r)
    assert row.index("Yes") < row.index("[") < row.index("No")


def test_the_focus_of_an_open_box_can_be_taken_away(backend):
    results = []
    panel = Panel(backend)
    box = show_message_box(panel, "Go?", buttons=("Yes", "No"), on_result=results.append)
    box.default = box.focused = None
    panel.render()
    panel.dispatch_event(_key("enter"))
    assert results == []
    panel.dispatch_event(_key("left"))
    panel.dispatch_event(_key("enter"))
    assert results == ["Yes"]


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


def test_message_box_marks_its_default_by_focus_alone(backend):
    """Enter presses the focused button, so no button wears the accent fill:
    the focus ring is the one cue, and the default is simply where focus
    starts."""
    panel = Panel(backend)
    show_message_box(panel, "Delete?", buttons=("Delete", "Cancel"), default=1)
    box = panel._layers[-1].widget
    assert [b.variant for b in box._widgets] == ["secondary", "secondary"]
    assert box.focused == 1


_CMD = "```\n" + "xefmctl state --json 2>/dev/null || xefmctl help " * 3 + "\n```"


class _TallCodeBackend(MemoryBackend):
    """Code lines taller than a base unit, as on a desktop backend whose
    monospace face has a taller line box than the UI font."""

    def measure_line_height(self, style=None):
        font = getattr(style, "font", None)
        return 1.4 if font is not None and font.monospace else 1.0


class _TallCodeVectorBackend(_TallCodeBackend):
    """The same on a vector backend (the base MemoryBackend forces
    ``vector_shapes`` off), where the box is sized to the measured rhythm."""

    @property
    def capabilities(self):
        return self._capabilities

    @property
    def base_size(self):
        return (8, 16)


@pytest.mark.parametrize("make", [
    lambda: _TallCodeBackend(width=60, height=20, capabilities=PROFILE_TUI),
    lambda: _TallCodeVectorBackend(width=60, height=20, capabilities=PROFILE_GUI_DESKTOP),
], ids=["tui", "vector"])
def test_a_markdown_message_with_a_wrapping_code_block_is_not_cut_short(make):
    panel = Panel(make())
    msg = "**Claude Code** wants to use **Bash**.\n\nGet detailed state\n\n" + _CMD
    box = show_message_box(panel, msg, buttons=["Allow", "Deny"], markdown=True)
    panel.render()
    x0, y0, x1, y1 = box._md_rect
    md = box._md
    assert md._row_tops[-1] <= (y1 - y0) + 1e-6  # all of it fits: nothing hidden
    assert md.offset == 0


def test_a_message_taller_than_the_screen_scrolls(backend):
    panel = Panel(backend)
    msg = "\n\n".join(f"line {n}" for n in range(60))
    box = show_message_box(panel, msg, buttons=["OK"], markdown=True)
    panel.render()
    _w, h = panel.backend.size_units
    md = box._md
    assert md._row_tops[-1] > box._md_rect[3] - box._md_rect[1]
    panel.dispatch_event(Event(type=EventType.KEY, key="pagedown"))
    panel.render()
    assert md.offset > 0
    x0, y0, _x1, _y1 = box._md_rect
    top = md.offset
    box.handle_event(Event(type=EventType.MOUSE_SCROLL, x=x0 + 1, y=y0 + 1, scroll=3))
    assert md.offset < top


def test_a_plain_line_wider_than_the_screen_wraps(backend):
    """A long path is shown whole, broken at the box's width, not cut off at
    the frame - what the user cannot see they cannot agree to."""
    panel = Panel(backend)
    path = "/Users/someone/projects/" + "/".join(f"folder{i:02d}" for i in range(12)) + "/end.txt"
    box = show_message_box(panel, f"Read {path}?", buttons=("Allow", "Deny"))
    panel.render()
    rows = box._lines()
    assert len(rows) > 1 and "".join(rows).replace(" ", "") == f"Read{path}?"
    text = "".join(backend.snapshot())
    assert "end.txt?" in text and "folder11" in text


def test_a_plain_message_that_fits_is_left_as_written(backend):
    panel = Panel(backend)
    box = show_message_box(panel, "One line.\nAnother.", buttons=("OK",))
    assert box._lines() == ["One line.", "Another."]

