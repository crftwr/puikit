"""Tests for the JsonView widget, run against TUI and GUI memory profiles."""

import pytest

from puikit import (CapabilityProfile, Event, EventType, Panel,
                   PROFILE_GUI_DESKTOP, PROFILE_TUI)
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import JsonView


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=40, height=14, capabilities=request.param)


class _VectorBackend(MemoryBackend):
    """A grid backend that *claims* vector_shapes and records the vector
    primitives, so the Panel's vector path can be tested headlessly (the real
    MemoryBackend masks the capability off — it cannot render vectors)."""

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile({**self._capabilities, "vector_shapes": True})


def _key(name, mods=frozenset()):
    return Event(type=EventType.KEY, key=name, modifiers=mods)


def _data():
    return {
        "name": "xefm",
        "tags": ["tui", "files"],
        "nested": {"ok": True, "count": 42, "z": None},
    }


def test_top_level_keys_render_with_markers(backend):
    panel = Panel(backend)
    panel.add(JsonView(_data()), x=0, y=0, w=40, h=14)
    panel.render()
    snap = backend.snapshot()
    assert snap[0].startswith('  name: "xefm"')     # scalar leaf, no expander
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


def test_branches_draw_vector_chevrons_not_glyphs():
    view = JsonView(_data())
    view.roots[1].expanded = True          # expand "tags" (its children are scalars)
    be = _VectorBackend(width=40, height=14, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(be)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    # Visible branches: "tags" (expanded) + "nested" (collapsed) → 2 chevrons;
    # the scalar "name" and tags' scalar items draw none.
    assert len(be.chevron_calls) == 2
    assert sorted(call[4] for call in be.chevron_calls) == [False, True]
    text = "\n".join(be.snapshot())
    assert "▸" not in text and "▾" not in text   # the glyph is a vector stroke now
    assert 'name: "xefm"' in text                 # labels still render


def test_scalar_document_draws_no_chevron():
    be = _VectorBackend(width=40, height=14, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(be)
    panel.add(JsonView([1, 2, 3]), x=0, y=0, w=40, h=14)   # scalar leaves only
    panel.render()
    assert be.chevron_calls == []


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


def test_search_jump_centers_offscreen_match(backend):
    # 30 top-level rows in a 14-row viewport. A match outside the comfort band
    # is centered — row 20 lands with int((14 - 1) / 2) = 6 rows above — while
    # a match already comfortably visible (row 4) leaves the scroll alone
    # (the shared search-jump rule, widgets/_scroll.py).
    view = JsonView({f"k{i:02d}": i for i in range(30)})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    view.search_begin()
    assert view.search_set("k04") == 1
    assert view.selected == 4 and view.offset == 0.0
    assert view.search_set("k20") == 1
    assert view.selected == 20 and view.offset == 14.0


def test_search_moves_selection_and_commits_on_accept(backend):
    # Like the main file manager's i-search: the selection follows the match and
    # Enter (search_accept) leaves it on the matched node.
    view = JsonView({"a": 1, "b": 2, "nested": {"target": 99}, "c": 3})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    assert view.selected == 0
    view.search_begin()
    view.search_set("target")
    assert view._visible()[view.selected][0].key == "target"   # selection on match
    view.search_accept()                                        # Enter
    assert view._visible()[view.selected][0].key == "target"   # stays there


def test_search_cancel_restores_selection(backend):
    view = JsonView({"a": 1, "b": 2, "nested": {"target": 99}, "c": 3})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    origin = view._visible()[view.selected][0]     # the node selected before search
    view.search_begin()
    view.search_set("target")
    assert view._visible()[view.selected][0].key == "target"
    view.search_cancel()                            # Esc restores the origin node
    assert view._visible()[view.selected][0] is origin


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


def test_unwrapped_long_value_is_cut_at_the_edge(backend):
    # Default (no wrap, no pan): the overflow is simply cut at the width — one
    # display line per row, the tail reachable by panning or wrapping.
    panel = Panel(backend)
    panel.add(JsonView({"msg": "a" * 60}), x=0, y=0, w=40, h=14)
    panel.render()
    snap = backend.snapshot()
    assert snap[0] == '  msg: "' + "a" * 32
    assert snap[1].strip() == ""


def test_shift_right_pans_and_shift_left_returns(backend):
    view = JsonView({"msg": "a" * 60})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_key("right", frozenset({"shift"})))
    panel.render()
    assert view.left == 4.0
    assert backend.snapshot()[0] == 'g: "' + "a" * 36   # window starts at col 4
    panel.dispatch_event(_key("left", frozenset({"shift"})))
    panel.render()
    assert view.left == 0.0
    assert backend.snapshot()[0].startswith('  msg: "')
    # Plain arrows keep their tree meaning: no pan, still expand/collapse.
    panel.dispatch_event(_key("right"))
    assert view.left == 0.0


def test_pan_clamps_to_the_widest_row(backend):
    # Content is 43 columns in a 40-column view: the pan stops at 3, with the
    # very tail of the value (the closing quote) at the right edge.
    view = JsonView({"msg": "a" * 30 + "TAIL"})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    for _ in range(3):
        panel.dispatch_event(_key("right", frozenset({"shift"})))
    assert view.left == 3.0
    panel.render()
    assert backend.snapshot()[0] == 'sg: "' + "a" * 30 + 'TAIL"'


def test_toggle_wrap_folds_long_value(backend):
    # Wrap on: the 60-char value continues on a second display line aligned
    # under the label; wrap off restores one line per row.
    view = JsonView({"msg": "a" * 60})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    assert view.toggle_wrap() is True
    panel.render()
    snap = backend.snapshot()
    assert snap[0].startswith('  msg: "' + "a" * 31)   # wrapped at 39 - indent
    assert snap[1].startswith("  " + "a" * 29 + '"')   # continuation, full tail
    assert view.toggle_wrap() is False
    panel.render()
    assert backend.snapshot()[1].strip() == ""


def test_wrap_cuts_wide_chars_by_display_columns(backend):
    # A CJK value overflows twice as fast as its character count; wrapping must
    # cut by columns so every glyph lands somewhere (cf. text viewer issue #315).
    view = JsonView({"k": "あ" * 30})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    assert "".join(backend.snapshot()).count("あ") < 30   # unwrapped: cut
    view.toggle_wrap()
    panel.render()
    assert "".join(backend.snapshot()).count("あ") == 30  # wrapped: all visible


def test_wrap_scrolls_by_display_lines(backend):
    # 10 rows of 2 display lines each = 20 lines in a 14-line viewport; End
    # keeps the last row's *bottom* line in view.
    view = JsonView({f"k{i}": "a" * 60 for i in range(10)})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    view.toggle_wrap()
    panel.render()
    assert len(view._lines) == 20
    panel.dispatch_event(_key("end"))
    assert view.selected == 9
    assert view.offset == 6.0                             # 20 lines - 14 visible


def test_click_on_wrapped_continuation_selects_its_row(backend):
    view = JsonView({"a": "a" * 60, "b": 1})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    view.toggle_wrap()
    panel.render()
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=5, y=1, button="left"))
    assert view.selected == 0                             # continuation of "a"
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=5, y=2, button="left"))
    assert view.selected == 1                             # "b", below the wrap


def test_wrap_draws_vector_chevron_on_first_line_only():
    # A wrapped branch row still gets exactly one chevron, on its first display
    # line; continuation lines draw none.
    view = JsonView({"nested": {"msg": "a" * 60}})
    view.roots[0].expanded = True
    be = _VectorBackend(width=40, height=14, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(be)
    panel.add(view, x=0, y=0, w=40, h=14)
    view.toggle_wrap()
    panel.render()
    assert len(be.chevron_calls) == 1          # "nested" only; "msg" is a leaf
    assert "a" * 20 in "\n".join(be.snapshot())  # the wrapped value renders


def test_search_highlight_survives_a_wrap_boundary(backend):
    # The match straddles two wrapped chunks; both fragments render and the
    # highlight pass draws without error.
    view = JsonView({"msg": "x" * 28 + "needle" + "y" * 20})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    view.toggle_wrap()
    panel.render()
    view.search_begin()
    assert view.search_set("needle") == 1
    panel.render()
    text = "".join(backend.snapshot())
    assert "nee" in text and "dle" in text                # split across lines


# --- structural mouse selection (fragment) -----------------------------------
#
# Row geometry at the defaults: depth-0 labels start at column 2 (indent 0 +
# the 2-column marker slot), depth-1 at column 4. Row 0 is '  name: "xefm"' —
# key chars at columns 2-5, the ': ' at 6-7, the value at 8-13.


def _mouse(kind, x, y):
    return Event(type=kind, x=x, y=y, button="left")


def test_click_selects_key_value_or_member(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 3, 0))   # on the key text
    assert view.fragment_text() == '"name"'
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 9, 0))   # on the value text
    assert view.fragment_text() == '"xefm"'
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 6, 0))   # on the ': '
    assert view.fragment_text() == '"name": "xefm"'
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 30, 0))  # past the label
    assert view.fragment_text() == '"name": "xefm"'


def test_click_on_branch_summary_selects_subdocument(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 11, 2))  # the '{3}' summary
    assert view.fragment_text() == '{"ok": true, "count": 42, "z": null}'


def test_drag_key_to_value_widens_to_member(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_DOWN, 3, 0))
    panel.dispatch_event(_mouse(EventType.MOUSE_DRAG, 9, 0))
    panel.dispatch_event(_mouse(EventType.MOUSE_UP, 9, 0))
    assert view.fragment_text() == '"name": "xefm"'


def test_drag_across_siblings_selects_their_container(backend):
    view = JsonView(_data())
    view.roots[2].expanded = True          # rows: name, tags, nested, ok, count, z
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_DOWN, 5, 3))    # on "ok"
    panel.dispatch_event(_mouse(EventType.MOUSE_DRAG, 5, 4))    # onto "count"
    panel.dispatch_event(_mouse(EventType.MOUSE_UP, 5, 4))
    assert view.fragment_text() == '{"ok": true, "count": 42, "z": null}'


def test_drag_from_branch_row_into_child_selects_member(backend):
    view = JsonView(_data())
    view.roots[2].expanded = True
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_DOWN, 3, 2))    # on "nested"
    panel.dispatch_event(_mouse(EventType.MOUSE_DRAG, 5, 3))    # into "ok"
    panel.dispatch_event(_mouse(EventType.MOUSE_UP, 5, 3))
    assert view.fragment_text() == '"nested": {"ok": true, "count": 42, "z": null}'


def test_drag_across_top_level_entries_selects_document(backend):
    import json
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_DOWN, 3, 0))    # "name"
    panel.dispatch_event(_mouse(EventType.MOUSE_DRAG, 3, 1))    # onto "tags"
    panel.dispatch_event(_mouse(EventType.MOUSE_UP, 3, 1))
    assert view.fragment_text() == json.dumps(_data(), ensure_ascii=False)


def test_array_element_offers_only_its_value(backend):
    # An index is not JSON text: clicking it (or the value) selects the value.
    view = JsonView(_data())
    view.roots[1].expanded = True          # rows: name, tags, 0: "tui", 1: ...
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 4, 2))   # on the index
    assert view.fragment_text() == '"tui"'
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 9, 2))   # on the value
    assert view.fragment_text() == '"tui"'


def test_ctrl_c_copies_the_fragment_then_falls_back(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 3, 0))   # key fragment
    panel.dispatch_event(_key("c", frozenset({"ctrl"})))
    assert backend.get_clipboard() == '"name"'
    panel.dispatch_event(_key("down"))                          # nav clears it
    assert view.fragment_text() is None
    panel.dispatch_event(_key("c", frozenset({"ctrl"})))        # old behavior
    assert backend.get_clipboard() == '["tui", "files"]'


def test_click_on_empty_space_clears_fragment(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 3, 0))
    assert view.fragment_text() == '"name"'
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 5, 12))  # below the rows
    assert view.fragment_text() is None


def test_fragment_click_works_on_wrapped_continuation(backend):
    # With wrap on, a click on a continuation line of a long string selects
    # the whole string value.
    view = JsonView({"msg": "a" * 60})
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    view.toggle_wrap()
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 5, 1))
    assert view.fragment_text() == '"' + "a" * 60 + '"'


def test_fragment_highlight_uses_selection_background(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_mouse(EventType.MOUSE_CLICK, 9, 1))   # tags summary
    panel.render()
    theme = panel.theme
    sel = (theme.text_selection_bg, theme.text_selection_inactive_bg)
    assert backend.style_at(9, 1).bg in sel     # the '[2]' summary is painted
    assert backend.style_at(3, 1).bg not in sel # the key isn't (value fragment)


def test_ctrl_c_copies_selected_value(backend):
    view = JsonView(_data())
    panel = Panel(backend)
    panel.add(view, x=0, y=0, w=40, h=14)
    panel.render()
    panel.dispatch_event(_key("c", frozenset({"ctrl"})))   # selected = "name"
    assert backend.get_clipboard() == '"xefm"'
    panel.dispatch_event(_key("down"))                     # select "tags"
    panel.dispatch_event(_key("c", frozenset({"cmd"})))
    assert backend.get_clipboard() == '["tui", "files"]'
