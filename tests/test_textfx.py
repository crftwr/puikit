"""Arriving-text animations (``puikit.textfx``).

The system has three design goals, and most of this file exists to hold them:

1. **A theme turns it on** — no app code decides, so it can be per-theme.
2. **A widget pays nothing** — the effect applies inside ``draw_text``, so the
   tests below animate an *unmodified* ``Label`` and a plain many-row widget.
3. **A new animation costs one function** — a pure kind in ``TEXT_EFFECTS``.

The trigger policy gets the most coverage, because the obvious rule (animate
whenever text changes) is the wrong one: it would re-decode a whole file pane on
every scroll step and re-animate a status counter every frame.
"""

from dataclasses import replace

import pytest

from puikit import PROFILE_GUI_DESKTOP, PROFILE_TUI, Panel, Style, theme_for
from puikit import textfx
from puikit.backends.memory_backend import MemoryBackend
from puikit.text import display_width
from puikit.textfx import TEXT_EFFECTS, TextEffect, coerce
from puikit.widgets import Label
from puikit.widgets.base import Widget

DECODE = {"kind": "decode", "duration_ms": 400}
ALL_KINDS = sorted(TEXT_EFFECTS)


class Rows(Widget):
    """Stands in for FilePane / LogView: many strings from one widget, and — the
    point — no text-animation code of its own."""

    def __init__(self, n=6, text=None):
        self.n = n
        self.text = text or (lambda i: f"row-{i:02d}-content")

    def draw(self, ctx):
        for i in range(self.n):
            ctx.draw_text(0, i, self.text(i), Style())


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def caps(request):
    return request.param


def make_panel(caps, effect=None, w=30, h=8):
    """A panel whose theme carries ``effect``. The theme is copied with a FRESH
    extras dict: ``theme_for`` returns a process-wide singleton, so mutating its
    extras in place would leak the effect into every other test."""
    be = MemoryBackend(width=w, height=h, capabilities=caps)
    panel = Panel(be)
    base = theme_for(be.capabilities)
    extras = dict(base.extras)
    if effect is not None:
        extras["text_effect"] = effect
    panel.theme = replace(base, extras=extras)
    return panel, be


def settle(panel):
    """Fast-forward every running text animation past its end."""
    for wid in list(panel._text_anims):
        panel._text_anims[wid] -= 99.0
    panel.render()


def line(be, row=0):
    return be.snapshot()[row].rstrip()


# --- the kinds ------------------------------------------------------------------

class TestKinds:

    @pytest.mark.parametrize("name", ALL_KINDS)
    def test_completed_progress_is_the_exact_source(self, name):
        # Whatever a kind does mid-flight, it must land on the real string.
        fn = TEXT_EFFECTS[name]
        assert fn("MISSION CONTROL", 1.0, 0, {}) == "MISSION CONTROL"
        assert fn("MISSION CONTROL", 5.0, 0, {}) == "MISSION CONTROL"

    @pytest.mark.parametrize("name", ALL_KINDS)
    def test_empty_text_survives(self, name):
        assert TEXT_EFFECTS[name]("", 0.5, 0, {}) == ""

    @pytest.mark.parametrize("name", ALL_KINDS)
    def test_is_deterministic_for_the_same_frame(self, name):
        # A redraw of one instant (resize, expose) must not reshuffle its noise.
        fn = TEXT_EFFECTS[name]
        assert fn("MISSION CONTROL", 0.4, 7, {}) == fn("MISSION CONTROL", 0.4, 7, {})

    @pytest.mark.parametrize("name", ALL_KINDS)
    @pytest.mark.parametrize("text", ["report.txt", "設定ファイル.txt", "写真_2024.png"])
    def test_rendered_width_never_moves(self, name, text):
        # The invariant every kind owes the layout: a mixed-width string must not
        # reflow mid-flight, or a CJK filename jitters as it arrives.
        fn = TEXT_EFFECTS[name]
        final = display_width(text)
        for i in range(21):
            assert display_width(fn(text, i / 20, i, {})) <= final

    def test_decode_fills_the_tail_with_junk(self):
        shown = textfx.decode("ABCDEFGHIJ", 0.5, 3, {})
        assert shown[:5] == "ABCDE"
        assert len(shown) == 10 and shown[5:] != "FGHIJ"

    def test_typewriter_leaves_the_tail_empty(self):
        assert textfx.typewriter("ABCDEFGHIJ", 0.5, 0, {}) == "ABCDE"

    def test_wipe_holds_the_tail_open(self):
        shown = textfx.wipe("ABCDEFGHIJ", 0.5, 0, {})
        assert shown.startswith("ABCDE") and set(shown[5:]) == {"░"}

    def test_wipe_fill_is_configurable(self):
        assert set(textfx.wipe("ABCDEFGHIJ", 0.5, 0, {"fill": "▒"})[5:]) == {"▒"}

    def test_flicker_keeps_the_text_present_throughout(self):
        # Unlike a reveal, flicker never hides characters — it disturbs them.
        assert len(textfx.flicker("MISSION CONTROL", 0.1, 4, {})) == len("MISSION CONTROL")

    def test_flicker_calms_as_it_progresses(self):
        text = "MISSION CONTROL SYSTEMS NOMINAL"
        early = sum(a != b for a, b in zip(text, textfx.flicker(text, 0.05, 2, {})))
        late = sum(a != b for a, b in zip(text, textfx.flicker(text, 0.9, 2, {})))
        assert early > late

    def test_adding_a_kind_is_one_function(self):
        # The requirement stated as a test: registering a callable is the whole
        # cost of a new animation — no dataclass, no Panel branch, no backend.
        TEXT_EFFECTS["shout"] = lambda t, p, f, params: t.upper() if p < 1 else t
        try:
            assert coerce("shout").fn("quiet", 0.5, 0, {}) == "QUIET"
        finally:
            del TEXT_EFFECTS["shout"]


# --- the descriptor -------------------------------------------------------------

class TestCoerce:

    def test_none_and_false_disable(self):
        assert coerce(None) is None and coerce(False) is None

    def test_true_gives_the_default_effect(self):
        assert coerce(True).kind == "decode"

    def test_a_bare_name(self):
        assert coerce("typewriter").kind == "typewriter"

    def test_a_params_dict(self):
        eff = coerce({"kind": "wipe", "duration_ms": 900, "stagger_ms": 30})
        assert (eff.kind, eff.duration_ms, eff.stagger_ms) == ("wipe", 900, 30)

    def test_kind_knobs_may_be_written_inline(self):
        assert coerce({"kind": "wipe", "fill": "▒"}).params["fill"] == "▒"

    def test_nested_params_also_work(self):
        assert coerce({"kind": "wipe", "params": {"fill": "▒"}}).params["fill"] == "▒"

    def test_unknown_kind_disables_rather_than_raising(self):
        # Themes and user config files are data; a typo should cost the animation,
        # not take down the app.
        assert coerce("no_such_kind") is None
        assert coerce({"kind": "nope"}) is None

    def test_zero_duration_is_a_noop(self):
        assert coerce({"kind": "decode", "duration_ms": 0}) is None

    def test_an_effect_passes_through(self):
        eff = TextEffect(kind="typewriter")
        assert coerce(eff) is eff

    def test_junk_input_disables(self):
        assert coerce(12345) is None


# --- theme gating ---------------------------------------------------------------

class TestThemeGating:

    def test_a_theme_without_an_effect_draws_plain_text(self, caps):
        panel, be = make_panel(caps)
        panel.add(Label("PLAIN THEME"), x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) == "PLAIN THEME"

    def test_a_theme_with_an_effect_animates(self, caps):
        panel, be = make_panel(caps, DECODE)
        panel.add(Label("SCIFI THEME"), x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) != "SCIFI THEME"

    def test_it_settles_on_the_exact_text(self, caps):
        panel, be = make_panel(caps, DECODE)
        panel.add(Label("SCIFI THEME"), x=0, y=0, w=30, h=1)
        panel.render()
        settle(panel)
        assert line(be) == "SCIFI THEME"

    def test_switching_theme_turns_it_on_with_no_app_code(self, caps):
        panel, be = make_panel(caps)
        lbl = Label("SWITCHED")
        panel.add(lbl, x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) == "SWITCHED"
        panel.theme = replace(panel.theme,
                              extras={**panel.theme.extras, "text_effect": DECODE})
        panel.animate_text(lbl)
        panel.render()
        assert line(be) != "SWITCHED"

    def test_explicit_override_beats_the_theme(self, caps):
        panel, be = make_panel(caps)
        panel.set_text_effect(DECODE)
        panel.add(Label("OVERRIDDEN"), x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) != "OVERRIDDEN"

    def test_reduced_motion_disables_it_entirely(self, caps):
        panel, be = make_panel(caps, DECODE)
        panel.set_reduced_motion(True)
        panel.add(Label("QUIET PLEASE"), x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) == "QUIET PLEASE"
        assert panel.text_effect is None


# --- the trigger policy ---------------------------------------------------------

class TestTriggerPolicy:
    """On-appear, deliberately — NOT on-change."""

    def test_a_widget_animates_when_it_appears(self, caps):
        panel, be = make_panel(caps, DECODE)
        panel.add(Label("ARRIVED"), x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) != "ARRIVED"

    def test_text_changing_in_place_does_NOT_retrigger(self, caps):
        # The case that decides whether this is usable: scrolling a list, or a
        # status bar counting up, changes text every frame. Re-animating there
        # would make the UI permanently unreadable.
        panel, be = make_panel(caps, DECODE)
        lbl = Label("ROW ONE")
        panel.add(lbl, x=0, y=0, w=30, h=1)
        panel.render()
        settle(panel)
        lbl.text = "ROW TWO"
        panel.render()
        assert line(be) == "ROW TWO"

    def test_repeated_renders_do_not_retrigger(self, caps):
        panel, be = make_panel(caps, DECODE)
        panel.add(Label("STEADY"), x=0, y=0, w=30, h=1)
        panel.render()
        settle(panel)
        for _ in range(5):
            panel.render()
            assert line(be) == "STEADY"

    def test_animate_text_replays_it(self, caps):
        panel, be = make_panel(caps, DECODE)
        lbl = Label("RELOADED")
        panel.add(lbl, x=0, y=0, w=30, h=1)
        panel.render()
        settle(panel)
        assert panel.animate_text(lbl) is True
        panel.render()
        assert line(be) != "RELOADED"

    def test_animate_text_reports_false_when_no_effect_is_active(self, caps):
        panel, _be = make_panel(caps)
        lbl = Label("X")
        panel.add(lbl, x=0, y=0, w=30, h=1)
        panel.render()
        assert panel.animate_text(lbl) is False

    def test_a_widget_can_opt_out(self, caps):
        class Field(Label):
            animates_text = False

        panel, be = make_panel(caps, DECODE)
        panel.add(Field("user@host"), x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) == "user@host"

    def test_the_tick_keeps_running_while_text_arrives(self, caps):
        panel, _be = make_panel(caps, DECODE)
        panel.add(Label("BRIEF"), x=0, y=0, w=30, h=1)
        panel.render()
        assert panel._animation_tick() is True

    def test_the_tick_releases_once_text_has_arrived(self, caps):
        # Rewound rather than slept: ticks on a memory backend are far faster
        # than any real duration, so racing the wall clock would be flaky.
        panel, _be = make_panel(caps, DECODE)
        panel.add(Label("BRIEF"), x=0, y=0, w=30, h=1)
        panel.render()
        for wid in list(panel._text_anims):
            panel._text_anims[wid] -= 99.0
        assert panel._animation_tick() is False
        assert not panel._text_anims


# --- many-string widgets --------------------------------------------------------

class TestManyStrings:
    """FilePane / LogView scale — one widget, dozens of strings, no widget code."""

    def test_every_string_of_a_plain_widget_participates(self, caps):
        panel, be = make_panel(caps, DECODE, h=6)
        panel.add(Rows(6), x=0, y=0, w=30, h=6)
        panel.render()
        assert all(line(be, i) != f"row-{i:02d}-content" for i in range(6))
        settle(panel)
        assert all(line(be, i) == f"row-{i:02d}-content" for i in range(6))

    def test_costs_one_animation_entry_per_widget_not_per_string(self, caps):
        # The scaling property: 200 strings must not mean 200 channels.
        panel, _be = make_panel(caps, DECODE, h=6)
        panel.add(Rows(6), x=0, y=0, w=30, h=6)
        panel.render()
        assert len(panel._text_anims) == 1

    def test_stagger_makes_earlier_strings_finish_first(self, caps):
        panel, be = make_panel(caps, {"kind": "typewriter", "duration_ms": 200,
                                      "stagger_ms": 250}, h=6)
        panel.add(Rows(6), x=0, y=0, w=30, h=6)
        panel.render()
        panel._text_anims[next(iter(panel._text_anims))] -= 0.30
        panel.render()
        assert line(be, 0) == "row-00-content"      # done
        assert line(be, 5) != "row-05-content"      # not started

    def test_max_strings_caps_the_cascade(self, caps):
        panel, be = make_panel(caps, {"kind": "decode", "duration_ms": 400,
                                      "max_strings": 3}, h=6)
        panel.add(Rows(6), x=0, y=0, w=30, h=6)
        panel.render()
        assert all(line(be, i) != f"row-{i:02d}-content" for i in range(3))
        # Past the cap the text lands complete rather than queueing an animation
        # the user would wait seconds to see finish.
        assert all(line(be, i) == f"row-{i:02d}-content" for i in range(3, 6))

    def test_equal_length_rows_get_distinct_noise(self, caps):
        # Without decorrelating per row, same-length strings scramble identically
        # and a pane reads as a repeating pattern rather than as data.
        panel, be = make_panel(caps, DECODE, h=6)
        panel.add(Rows(6, text=lambda i: "SAME-LENGTH-ROW"), x=0, y=0, w=30, h=6)
        panel.render()
        rows = [line(be, i) for i in range(6)]
        assert len(set(rows)) == 6


# --- content-level trigger (streams) --------------------------------------------

class TestArrivingContent:
    """A log stream is the case the appear trigger cannot serve: the widget never
    leaves the screen while its content keeps arriving. Such a widget names each
    string's identity via ``draw_text(anim_key=...)``."""

    def _log(self, caps, effect=None, lines=5, **kw):
        from puikit.widgets import LogView
        panel, be = make_panel(caps, effect or {"kind": "typewriter",
                                                "duration_ms": 300}, w=30, h=5)
        log = LogView(max_lines=kw.pop("max_lines", 100), auto_scroll=True)
        for i in range(lines):
            log.append(f"line-{i}-existing")
        panel.add(log, x=0, y=0, w=30, h=5)
        return panel, be, log

    def _settle(self, panel):
        for in_flight in panel._text_key_anims.values():
            for key in in_flight:
                in_flight[key] -= 99.0
        panel._animation_tick()

    def test_visible_lines_animate_when_the_view_first_appears(self, caps):
        panel, be, _log = self._log(caps)
        panel.render()
        assert all(line(be, i) != f"line-{i}-existing" for i in range(5))

    def test_an_appended_line_animates(self, caps):
        panel, be, log = self._log(caps)
        panel.render()
        self._settle(panel)
        log.append("BRAND NEW")
        panel.render()
        assert "BRAND NEW" not in "\n".join(be.snapshot())

    def test_only_the_appended_line_animates(self, caps):
        # The reason this is not just `animate_text(log)`: that would re-animate
        # every line already on screen and being read.
        panel, _be, log = self._log(caps)
        panel.render()
        self._settle(panel)
        log.append("BRAND NEW")
        panel.render()
        in_flight = list(panel._text_key_anims.values())[0]
        assert sorted(in_flight) == [5]

    def test_an_appended_line_settles_on_its_exact_text(self, caps):
        panel, be, log = self._log(caps)
        panel.render()
        self._settle(panel)
        log.append("BRAND NEW")
        panel.render()
        self._settle(panel)
        panel.render()
        assert "BRAND NEW" in "\n".join(be.snapshot())

    def test_scrolling_over_old_lines_never_re_animates(self, caps):
        panel, be, log = self._log(caps, lines=20)
        panel.render()
        self._settle(panel)
        for _ in range(4):
            log.scroll_by(-2)
            panel.render()
            assert not panel._text_key_anims, "a re-read line must not animate again"
            log.scroll_by(2)
            panel.render()
            assert not panel._text_key_anims

    def test_in_flight_keys_are_released_when_they_finish(self, caps):
        panel, _be, log = self._log(caps)
        panel.render()
        assert panel._text_key_anims
        self._settle(panel)
        assert not panel._text_key_anims, "finished lines must not be held forever"

    def test_state_is_one_mark_not_a_key_history(self, caps):
        # What bounds a stream running for days: the Panel keeps a high-water
        # mark per widget, never the set of every key it has drawn.
        panel, _be, log = self._log(caps, lines=0)
        for i in range(200):
            log.append(f"line-{i}")
            panel.render()
            self._settle(panel)
        assert len(panel._text_marks) == 1
        assert not panel._text_key_anims

    def test_identity_survives_buffer_trimming(self, caps):
        # A line's position shifts when the buffer is trimmed; keying on position
        # would make old lines look new and re-animate them.
        panel, _be, log = self._log(caps, lines=0, max_lines=64)
        marks = []
        for i in range(300):
            log.append(f"line-{i}")
            panel.render()
            self._settle(panel)
            marks.append(list(panel._text_marks.values())[0])
        assert log._dropped > 0, "the buffer should have been trimmed"
        assert all(b >= a for a, b in zip(marks, marks[1:])), "identity went backwards"
        assert marks[-1] == log._dropped + len(log.lines) - 1

    def test_a_line_appended_after_clear_still_animates(self, caps):
        # clear() resets the buffer; without advancing the identity counter the
        # next line would reuse a retired key and be treated as already seen.
        panel, _be, log = self._log(caps)
        panel.render()
        self._settle(panel)
        log.clear()
        log.append("AFTER CLEAR")
        panel.render()
        assert panel._text_key_anims, "the first line after a clear must animate"

    def test_wrapped_rows_of_one_line_share_a_key(self, caps):
        # So a folded line reveals as one line rather than each row racing.
        from puikit.widgets import LogView
        panel, _be = make_panel(caps, {"kind": "typewriter", "duration_ms": 300},
                                w=20, h=6)
        log = LogView(max_lines=100, wrap=True)
        log.append("a very long line that will certainly fold across several rows")
        panel.add(log, x=0, y=0, w=20, h=6)
        panel.render()
        assert sorted(list(panel._text_key_anims.values())[0]) == [0]

    def test_a_theme_without_an_effect_leaves_the_log_alone(self, caps):
        panel, be = make_panel(caps, None, w=30, h=5)
        from puikit.widgets import LogView
        log = LogView(max_lines=100, auto_scroll=True)
        log.append("plain line")
        panel.add(log, x=0, y=0, w=30, h=5)
        panel.render()
        assert "plain line" in "\n".join(be.snapshot())
        assert not panel._text_key_anims
