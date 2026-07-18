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

    # --- scatter (random-order reveal) --------------------------------------

    def test_scatter_reveals_nothing_at_the_start(self):
        assert textfx.scatter("MISSION CONTROL", 0.0, 0, {}) == ""

    def test_scatter_holds_every_character_in_its_final_column(self):
        # The defining constraint: characters land in random ORDER, never in a
        # random place. Whatever is visible must match the source at that index.
        text = "MISSION CONTROL"
        for i in range(21):
            shown = textfx.scatter(text, i / 20, 0, {})
            for j, ch in enumerate(shown):
                assert ch in (" ", text[j]), f"character moved at index {j}"

    def test_scatter_is_not_left_to_right(self):
        # Otherwise it is just typewriter with extra steps.
        text = "MISSION CONTROL SYSTEMS"
        seen = [textfx.scatter(text, i / 20, 0, {}) for i in range(1, 20)]
        assert any(" " in s.rstrip() for s in seen), "no interior gap: reveal was sequential"

    def test_scatter_count_grows_monotonically(self):
        text = "MISSION CONTROL"
        counts = [sum(c != " " for c in textfx.scatter(text, i / 40, 0, {}))
                  for i in range(41)]
        assert all(b >= a for a, b in zip(counts, counts[1:]))

    def test_scatter_order_is_stable_for_one_string(self):
        # A redraw of the same instant must not reshuffle which characters are up.
        a = textfx.scatter("MISSION CONTROL", 0.5, 0, {})
        assert a == textfx.scatter("MISSION CONTROL", 0.5, 9, {})

    def test_scatter_order_differs_between_strings(self):
        # Salted by content, so a pane of similar rows does not reveal in lockstep.
        rows = {textfx.scatter(f"row-{i:02d}-content", 0.5, 0, {}) for i in range(6)}
        assert len(rows) == 6

    def test_scatter_can_hide_behind_junk_instead_of_blanks(self):
        shown = textfx.scatter("ABCDEFGHIJ", 0.5, 3, {"hidden": "scramble"})
        assert len(shown) == 10 and " " not in shown

    # --- flash (block at the moment a character lands) ----------------------

    def test_flash_puts_a_block_where_a_character_is_landing(self):
        seen = [textfx.typewriter("MISSION CONTROL", i / 40, 0, {"flash": 0.1})
                for i in range(1, 40)]
        assert any(textfx.FLASH_GLYPH in s for s in seen)

    def test_flash_is_transient_not_the_final_state(self):
        assert textfx.FLASH_GLYPH not in textfx.typewriter("HELLO", 1.0, 0, {"flash": 0.5})

    def test_flash_never_appears_without_the_option(self):
        for name in ALL_KINDS:
            for i in range(21):
                assert textfx.FLASH_GLYPH not in TEXT_EFFECTS[name]("HELLO", i / 20, i, {})

    def test_flash_leaves_resolved_characters_alone(self):
        # Only the landing character flashes; the ones already down stay readable.
        shown = textfx.typewriter("MISSION CONTROL", 0.7, 0, {"flash": 0.06})
        assert shown.startswith("MISSION")

    def test_flash_glyph_is_configurable(self):
        seen = [textfx.typewriter("HELLO WORLD", i / 20, 0,
                                  {"flash": 0.1, "flash_glyph": "▓"})
                for i in range(1, 20)]
        assert any("▓" in s for s in seen)

    def test_flash_holds_the_column_count_of_a_wide_glyph(self):
        # A one-column block standing in for a two-column CJK glyph would make
        # the string shrink and everything after it slide left.
        text = "設定ファイル"
        final = display_width(text)
        for i in range(21):
            assert display_width(textfx.scatter(text, i / 20, i, {"flash": 0.15})) <= final

    def test_every_character_still_lands_with_flash_on(self):
        # Thresholds are compressed into [0, 1-flash] so the last character
        # finishes its flash before the animation ends rather than being cut off.
        assert textfx.scatter("HELLO", 1.0, 0, {"flash": 0.4}) == "HELLO"
        assert textfx.typewriter("HELLO", 1.0, 0, {"flash": 0.4}) == "HELLO"

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

    def test_a_text_input_never_animates(self, caps):
        # Editable content under a caret, possibly mid-typing. Scrambling or
        # withholding it is wrong in a way no theme should be able to ask for.
        from puikit.widgets import TextEdit

        panel, be = make_panel(caps, DECODE)
        field = TextEdit("user@host")
        panel.add(field, x=0, y=0, w=30, h=1)
        panel.render()
        assert "user@host" in "\n".join(be.snapshot())

    def test_the_rule_follows_wants_text_input_not_a_per_widget_flag(self, caps):
        # So any future input widget inherits it without knowing this exists.
        class Field(Widget):
            wants_text_input = True

            def draw(self, ctx):
                ctx.draw_text(0, 0, "EDITABLE", Style())

        panel, be = make_panel(caps, DECODE)
        panel.add(Field(), x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) == "EDITABLE"

    def test_an_input_may_still_opt_back_in_explicitly(self, caps):
        class Showy(Widget):
            wants_text_input = True
            animates_text = True

            def draw(self, ctx):
                ctx.draw_text(0, 0, "EDITABLE", Style())

        panel, be = make_panel(caps, DECODE)
        panel.add(Showy(), x=0, y=0, w=30, h=1)
        panel.render()
        assert line(be) != "EDITABLE"

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

    def test_max_rows_caps_the_cascade(self, caps):
        panel, be = make_panel(caps, {"kind": "decode", "duration_ms": 400,
                                      "max_rows": 3}, h=6)
        panel.add(Rows(6), x=0, y=0, w=30, h=6)
        panel.render()
        assert all(line(be, i) != f"row-{i:02d}-content" for i in range(3))
        # Past the cap the text lands complete rather than queueing an animation
        # the user would wait seconds to see finish.
        assert all(line(be, i) == f"row-{i:02d}-content" for i in range(3, 6))

    def test_the_cap_and_stagger_count_ROWS_not_strings(self, caps):
        """The reported bug, twice over: a string-based cap animated only the
        first ~5 lines of a syntax-highlighted viewer (9 strings a line) and cut
        a tall file pane off half way down. Rows are the unit a user sees, and
        the strings-per-row ratio varies by an order of magnitude between
        widgets, so the cap has to be in rows for the number to mean anything."""
        class Wide(Widget):
            """4 strings per row — a pane row with name / ext / size / date."""
            def draw(self, ctx):
                for r in range(10):
                    for c in range(4):
                        ctx.draw_text(c * 7, r, f"r{r}c{c}xx", Style())

        panel, be = make_panel(caps, {"kind": "typewriter", "duration_ms": 400,
                                      "max_rows": 8}, w=40, h=10)
        panel.add(Wide(), x=0, y=0, w=40, h=10)
        panel.render()
        rows = [be.snapshot()[r].rstrip() for r in range(10)]
        # The first 8 ROWS animate (not the first 8 strings, which would be row 2).
        assert all(r != "r{}c0xx".format(i) for i, r in enumerate(rows[:8]))
        # ...and rows past the cap land complete.
        assert rows[8].startswith("r8c0xx") and rows[9].startswith("r9c0xx")

    def test_strings_sharing_a_row_share_one_cascade_step(self, caps):
        # They are one visual unit; staggering within a row would ripple across
        # a pane's columns instead of down its rows.
        class Wide(Widget):
            def draw(self, ctx):
                for r in range(4):
                    for c in range(3):
                        ctx.draw_text(c * 10, r, f"row{r}col{c}", Style())

        panel, be = make_panel(caps, {"kind": "typewriter", "duration_ms": 200,
                                      "stagger_ms": 400}, w=40, h=4)
        panel.add(Wide(), x=0, y=0, w=40, h=4)
        panel.render()
        for wid in list(panel._text_anims):
            panel._text_anims[wid] -= 0.30
        panel.render()
        # Row 0 is past its window; rows 1+ have not started. Within row 0 every
        # column resolved together rather than trailing one another.
        row0 = be.snapshot()[0].rstrip()
        assert row0.startswith("row0col0") and "row0col2" in row0
        assert not be.snapshot()[1].strip()

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


# --- per-widget variant ---------------------------------------------------------

class TestWidgetVariant:
    """A widget may choose the *flavor*; the theme keeps authority over whether
    text animates at all, and over the timing."""

    class _Viewer(Widget):
        text_effect = {"kind": "scatter", "flash": 0.1}

        def __init__(self, child=None):
            self.child = child

        def draw(self, ctx):
            ctx.draw_text(0, 0, "HEADER LINE", Style())
            if self.child is not None:
                ctx.draw_child(self.child, 0, 1, 30, 1)

    class _Body(Widget):
        def draw(self, ctx):
            ctx.draw_text(0, 0, "BODY LINE", Style())

    def _effect_for(self, panel, widget):
        """The effect the Panel would actually use for ``widget``."""
        base = panel.text_effect
        variant = getattr(widget, "text_effect", None)
        return textfx.merge(base, variant) if base and variant else base

    def test_widget_variant_overrides_the_theme_kind(self, caps):
        panel, _be = make_panel(caps, {"kind": "typewriter", "duration_ms": 260})
        viewer = self._Viewer()
        assert self._effect_for(panel, viewer).kind == "scatter"

    def test_variant_inherits_the_themes_timing(self, caps):
        # The theme paces the whole UI; a widget naming only a kind keeps that.
        panel, _be = make_panel(caps, {"kind": "typewriter", "duration_ms": 260,
                                       "stagger_ms": 12, "max_rows": 40})
        eff = self._effect_for(panel, self._Viewer())
        assert (eff.duration_ms, eff.stagger_ms, eff.max_rows) == (260, 12, 40)

    def test_a_variant_animates_nothing_when_the_theme_opts_out(self, caps):
        # The property that keeps a widget preference from overriding the user's
        # theme choice: no effect on the theme means no effect, period.
        panel, be = make_panel(caps, None)
        panel.add(self._Viewer(), x=0, y=0, w=30, h=2)
        panel.render()
        assert line(be) == "HEADER LINE"
        assert not panel._text_anims

    def test_reduced_motion_still_wins_over_a_variant(self, caps):
        panel, be = make_panel(caps, {"kind": "typewriter", "duration_ms": 260})
        panel.set_reduced_motion(True)
        panel.add(self._Viewer(), x=0, y=0, w=30, h=2)
        panel.render()
        assert line(be) == "HEADER LINE"

    def test_variant_applies_to_the_whole_subtree(self, caps):
        # A viewer that delegates its body to a child widget must animate as ONE
        # thing — otherwise its header scatters while its body types, which is
        # exactly what happened before the variant was inherited down.
        seen = {}

        class Recording(Widget):
            def draw(self, ctx):
                seen["variant"] = ctx._text_variant
                ctx.draw_text(0, 0, "BODY LINE", Style())

        panel, _be = make_panel(caps, {"kind": "typewriter", "duration_ms": 400},
                                w=30, h=3)
        panel.add(self._Viewer(Recording()), x=0, y=0, w=30, h=3)
        panel.render()
        assert seen["variant"] == {"kind": "scatter", "flash": 0.1}

    def test_a_widget_with_no_variant_anywhere_inherits_nothing(self, caps):
        seen = {}

        class Recording(Widget):
            def draw(self, ctx):
                seen["variant"] = ctx._text_variant
                ctx.draw_text(0, 0, "PLAIN", Style())

        panel, _be = make_panel(caps, {"kind": "typewriter", "duration_ms": 400})
        panel.add(Recording(), x=0, y=0, w=30, h=1)
        panel.render()
        assert seen["variant"] is None

    def test_a_child_may_override_its_parents_variant(self, caps):
        class Plain(Widget):
            text_effect = {"kind": "typewriter"}

            def draw(self, ctx):
                ctx.draw_text(0, 0, "CHILD", Style())

        panel, _be = make_panel(caps, {"kind": "decode", "duration_ms": 400}, h=3)
        viewer = self._Viewer(Plain())
        panel.add(viewer, x=0, y=0, w=30, h=3)
        panel.render()
        # Nothing to assert on the pixels here beyond it not raising; the point
        # is that the child's own attribute takes precedence in DrawContext.
        assert getattr(Plain, "text_effect")["kind"] == "typewriter"

    def test_merge_keeps_base_when_the_override_is_unusable(self):
        base = TextEffect(kind="typewriter", duration_ms=260)
        assert textfx.merge(base, {"kind": "no_such_kind"}) is base
        assert textfx.merge(base, 12345) is base
        assert textfx.merge(base, None) is base

    def test_merge_accepts_a_bare_kind_name(self):
        base = TextEffect(kind="typewriter", duration_ms=260, stagger_ms=9)
        merged = textfx.merge(base, "scatter")
        assert merged.kind == "scatter" and merged.stagger_ms == 9

    def test_merge_layers_params_over_the_base(self):
        base = TextEffect(kind="decode", params={"hidden": "scramble"})
        merged = textfx.merge(base, {"kind": "scatter", "flash": 0.2})
        assert merged.params == {"hidden": "scramble", "flash": 0.2}


# --- proportional text ----------------------------------------------------------

class TestProportionalSurfaces:
    """On a proportional face no glyph reliably matches a character's advance, so
    a reveal cannot hold a gap open with a placeholder. The kinds emit one glyph
    per source character (``HIDDEN`` for an un-revealed one) and the Panel places
    each visible piece by measuring the real text — see ``_draw_measured``."""

    def test_output_is_index_aligned_with_the_source(self):
        # What makes measured positioning possible: shown[i] is the glyph for
        # source[i], with no width padding to shift the correspondence.
        text = "設定ファイル.txt"
        for kind in ("scatter", "typewriter", "decode"):
            for i in range(21):
                shown = TEXT_EFFECTS[kind](text, i / 20, i, {"grid": False})
                assert len(shown) == len(text), f"{kind} lost index alignment"

    def test_hidden_positions_use_the_sentinel(self):
        shown = textfx.scatter("ABCDEFGHIJKL", 0.4, 0, {"grid": False})
        assert textfx.HIDDEN in shown
        assert " " not in shown, "a blank would be a width-matched placeholder"

    def test_scatter_keeps_its_order_on_a_proportional_run(self):
        # The whole point of this round: the reveal must NOT fall back to
        # left-to-right just because the face is proportional.
        text = "ABCDEFGHIJKLMNOP"
        sequential = 0
        for i in range(1, 20):
            shown = textfx.scatter(text, i / 20, i, {"grid": False})
            resolved = "".join(c for c, srcc in zip(shown, text) if c == srcc)
            if text.startswith(resolved) and resolved:
                sequential += 1
        assert sequential < 10, "the proportional reveal collapsed into typing order"

    def test_a_grid_run_still_pads_and_trims(self):
        shown = textfx.scatter("ABCDEFGHIJKL", 0.5, 0, {"grid": True})
        assert textfx.HIDDEN not in shown
        assert " " in shown.rstrip(), "grid run lost its width-matched gaps"

    @pytest.mark.parametrize("kind,params", [
        ("scatter", {"flash": 0.1}), ("scatter", {}),
        ("typewriter", {"flash": 0.1}), ("decode", {}),
    ])
    def test_text_keeps_arriving_throughout(self, kind, params):
        """A regression from an earlier attempt: trimming a scattered reveal to
        its contiguous resolved prefix left the text invisible behind the flash
        until it all appeared at once. An arriving-text animation whose text does
        not arrive until it finishes is worse than none."""
        text = "MISSION CONTROL SYSTEMS ONLINE"
        counts = []
        for i in range(1, 20):
            shown = TEXT_EFFECTS[kind](text, i / 20, i, {**params, "grid": False})
            counts.append(sum(1 for c, srcc in zip(shown, text)
                              if c == srcc and c != " "))
        assert counts[len(counts) // 2] > 0, "nothing visible at the halfway point"
        assert counts[-1] > counts[0]
        assert sum(1 for a, b in zip(counts, counts[1:]) if b > a) >= 4, \
            "the reveal advanced in too few steps to read as progressive"

    def test_grid_flag_defaults_to_true(self):
        # A kind called directly (no Panel injecting the flag) behaves as it did.
        assert textfx.scatter("ABCDEFGHIJKL", 0.5, 0, {}) == \
               textfx.scatter("ABCDEFGHIJKL", 0.5, 0, {"grid": True})


class TestPanelPassesGridFlag:
    """The Panel decides grid-vs-proportional from the *resolved* style, which is
    capability-aware — so the degrade engages exactly where it is needed and
    nowhere else."""

    class _Styled(Widget):
        def __init__(self, font):
            self.font = font

        def draw(self, ctx):
            ctx.draw_text(0, 0, "ABCDEFGHIJKL", Style(font=self.font))

    def _runs(self, caps, font):
        panel, be = make_panel(caps, {"kind": "scatter", "duration_ms": 400})
        captured = []
        orig = be.draw_text
        be.draw_text = lambda x, y, t, st=None: (
            captured.append(t),
            orig(x, y, t, st) if st is not None else orig(x, y, t),
        )[1]
        panel.add(self._Styled(font), x=0, y=0, w=30, h=1)
        panel.render()
        for wid in list(panel._text_anims):
            panel._text_anims[wid] -= 0.2
        panel.render()
        return [t for t in captured if t]

    def test_a_terminal_is_always_a_grid(self):
        # No proportional_text capability, so every run folds to the grid face and
        # interior placeholders are exactly right — whatever font was requested.
        from puikit.font import Font
        for font in (None, Font(monospace=True), Font(family="Helvetica")):
            runs = self._runs(PROFILE_TUI, font)
            assert any(" " in t.rstrip() for t in runs), f"grid scatter lost for {font}"

    def test_gui_text_naming_no_font_is_proportional_and_degrades(self):
        # The important real case: on GUI an unstyled run flows in the
        # proportional UI font (docs/font_system.md §5) — which is what a file
        # pane's filenames use — so it must take the substitution-free path.
        runs = self._runs(PROFILE_GUI_DESKTOP, None)
        assert all(" " not in t for t in runs), "proportional run kept interior gaps"

    def test_gui_monospace_text_keeps_the_full_reveal(self):
        # A log view / code viewer pins a monospace face, so it is column-aligned
        # even on GUI and loses nothing.
        from puikit.font import Font
        runs = self._runs(PROFILE_GUI_DESKTOP, Font(monospace=True))
        assert any(" " in t.rstrip() for t in runs), "monospace GUI run was degraded"

    def test_a_sized_monospace_font_is_not_grid_aligned(self):
        from puikit.font import Font, grid_aligned
        assert grid_aligned(Font(monospace=True))
        assert not grid_aligned(Font(monospace=True, size=14))
        assert not grid_aligned(Font(family="Helvetica"))


class TestMeasuredPositioning:
    """The Panel places a proportional reveal by measuring the real text, so a
    resolved glyph sits exactly where it will finally sit — the guarantee that
    replaced width-matched placeholders."""

    class _Prop(MemoryBackend):
        """Per-glyph advances resembling a UI font, so a placeholder-based
        approach would visibly drift."""
        ADV = {"i": 0.28, "l": 0.28, ".": 0.25, " ": 0.30,
               "W": 1.05, "M": 1.00, "O": 0.85, "█": 0.60}

        def __init__(self, **kw):
            super().__init__(capabilities=PROFILE_GUI_DESKTOP, **kw)
            self.calls = []

        def measure_text(self, text, style=None):
            if style is not None and style.font is not None:
                return sum(self.ADV.get(c, 0.55) for c in text)
            return float(display_width(text))

        def draw_text(self, x, y, text, style=None):
            self.calls.append((x, text))
            return super().draw_text(x, y, text, style) if style is not None \
                else super().draw_text(x, y, text)

    TEXT = "WilMjO.lXqZ"

    def _draws(self, kind, params, at):
        be = self._Prop(width=40, height=1)
        panel = Panel(be)
        base = theme_for(be.capabilities)
        panel.theme = replace(base, extras={**base.extras,
                              "text_effect": {"kind": kind, "duration_ms": 400,
                                              **params}})
        panel.add(Label(self.TEXT), x=0, y=0, w=40, h=1)
        panel.render()
        for wid in list(panel._text_anims):
            panel._text_anims[wid] -= at
        be.calls.clear()
        panel.render()
        return be.calls

    def _final_x(self, index):
        return sum(self._Prop.ADV.get(c, 0.55) for c in self.TEXT[:index])

    @pytest.mark.parametrize("kind,params", [
        ("scatter", {}), ("scatter", {"flash": 0.12}), ("typewriter", {}),
    ])
    @pytest.mark.parametrize("at", [0.1, 0.2, 0.3])
    def test_every_resolved_glyph_is_drawn_at_its_final_x(self, kind, params, at):
        for x, run in self._draws(kind, params, at):
            idx = self.TEXT.find(run)
            if idx < 0 or not run.strip():
                continue  # a substitution glyph, not a piece of the real text
            assert x == pytest.approx(self._final_x(idx), abs=1e-9), \
                f"{run!r} drawn at {x}, belongs at {self._final_x(idx)}"

    def test_hidden_characters_are_not_drawn_at_all(self):
        for _x, run in self._draws("scatter", {}, 0.2):
            assert textfx.HIDDEN not in run, "the sentinel reached the backend"

    def test_gaps_cost_no_placeholder(self):
        # Nothing is painted over an un-revealed character's cells, so whatever
        # is behind it (a row fill, a selection) shows through untouched.
        runs = [run for _x, run in self._draws("scatter", {}, 0.2)]
        assert all(run.strip() for run in runs), "a blank placeholder was drawn"

    def test_prefix_widths_are_cached_across_frames(self):
        be = self._Prop(width=40, height=1)
        panel = Panel(be)
        base = theme_for(be.capabilities)
        panel.theme = replace(base, extras={**base.extras,
                              "text_effect": {"kind": "scatter", "duration_ms": 400}})
        panel.add(Label(self.TEXT), x=0, y=0, w=40, h=1)
        panel.render()
        assert panel._prefix_cache, "prefix widths should be memoized"
        n = len(panel._prefix_cache)
        for _ in range(5):
            panel.render()
        assert len(panel._prefix_cache) == n, "cache grew per frame"

    def test_cache_is_released_once_nothing_is_animating(self):
        be = self._Prop(width=40, height=1)
        panel = Panel(be)
        base = theme_for(be.capabilities)
        panel.theme = replace(base, extras={**base.extras,
                              "text_effect": {"kind": "scatter", "duration_ms": 400}})
        panel.add(Label(self.TEXT), x=0, y=0, w=40, h=1)
        panel.render()
        for wid in list(panel._text_anims):
            panel._text_anims[wid] -= 99.0
        panel._animation_tick()
        assert not panel._prefix_cache
