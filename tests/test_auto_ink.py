"""Panel.auto_ink: the opt-in draw-time legibility guarantee.

Renders text through a real Panel and reads back the fg that reached the cells,
so these exercise the DrawContext._text_style seam end to end (off by default,
weight-aware target, floor-only, transparent-fill skip)."""

from puikit import Item, Panel, VSplit
from puikit.backend import Style, TextAttribute, TRANSPARENT
from puikit.backends.memory_backend import MemoryBackend
from puikit.capability import PROFILE_GUI_DESKTOP
from puikit.color import LC_BODY, LC_MIN_NONTEXT, apca_lc, legible_ink
from puikit.theme import THEME_TUI
from puikit.widgets.base import Widget

CONTENT = THEME_TUI.surfaces["content"]   # (30, 30, 38)
DIM_FG = (66, 72, 86)                      # deliberately low-contrast on CONTENT


class _Ink(Widget):
    def __init__(self, fg, attr=TextAttribute.NORMAL, bg=None, ink=True):
        self.fg, self.attr, self.bg, self.ink = fg, attr, bg, ink

    def draw(self, ctx):
        ctx.draw_text(0, 0, "hello", Style(fg=self.fg, attr=self.attr, bg=self.bg), ink=self.ink)


def _render(fg, attr=TextAttribute.NORMAL, auto=True, bg=None, caps=None, ink=True):
    backend = MemoryBackend(width=12, height=3, **({"capabilities": caps} if caps else {}))
    panel = Panel(backend, theme=THEME_TUI)
    panel.auto_ink = auto
    panel.set_layout(VSplit(Item(_Ink(fg, attr, bg, ink), hints={"surface": "content"})))
    panel.render()
    return backend.style_at(0, 0).fg


def test_off_by_default_leaves_color_exact():
    assert DIM_FG == _render(DIM_FG, auto=False)


def test_normal_text_lifted_to_body_floor():
    got = _render(DIM_FG)
    assert got == legible_ink(DIM_FG, CONTENT, LC_BODY)
    assert abs(apca_lc(got, CONTENT)) >= LC_BODY - 0.5
    assert got != DIM_FG


def test_dim_text_kept_dimmer_than_body():
    body = _render(DIM_FG, TextAttribute.NORMAL)
    dim = _render(DIM_FG, TextAttribute.DIM)
    assert abs(apca_lc(dim, CONTENT)) >= LC_MIN_NONTEXT - 0.5      # not invisible
    assert abs(apca_lc(dim, CONTENT)) < abs(apca_lc(body, CONTENT))  # still recedes


def test_already_legible_color_untouched():
    bright = (230, 230, 240)
    assert abs(apca_lc(bright, CONTENT)) >= LC_BODY
    assert bright == _render(bright)


def test_transparent_fill_skipped_on_compositing_backend():
    # Over a transparent fill (compositing backend), the glyphs land on whatever
    # the widget painted underneath, which owns the contrast — auto-ink stays out.
    got = _render(DIM_FG, bg=TRANSPARENT, caps=PROFILE_GUI_DESKTOP)
    assert got == DIM_FG


def test_per_draw_ink_false_opts_out():
    # A run drawn with ink=False keeps its exact color even while auto_ink is on
    # (syntax highlighting, color legends); ink=True on the same color is lifted.
    assert _render(DIM_FG, ink=False) == DIM_FG
    assert _render(DIM_FG, ink=True) != DIM_FG


def test_frame_divider_matches_box_border_under_auto_ink():
    # A frame divider (a dialog's title-bar rule) is a structural line, not text:
    # with auto_ink on it must render the exact popup_border color, identical to
    # the box frame it joins — not get lifted to a text floor, which showed the
    # rule in a different color than the surrounding frame.
    from puikit.color import LC_BODY, apca_lc, legible_ink
    from puikit.theme import THEME_TUI

    border, bg = THEME_TUI.popup_border, THEME_TUI.popup_bg
    # Meaningful only if the border would otherwise be lifted (it's a dim line).
    assert abs(apca_lc(border, bg)) < LC_BODY
    assert legible_ink(border, bg, LC_BODY) != border

    class _Frame(Widget):
        def draw(self, ctx):
            ctx.draw_box(0, 0, ctx.width, ctx.height,
                         Style(bg=bg, fg=border), hints={"fill": True})
            ctx.draw_frame_divider(2, Style(fg=border, bg=bg))

    backend = MemoryBackend(width=12, height=5)
    panel = Panel(backend, theme=THEME_TUI)
    panel.auto_ink = True
    panel.set_layout(VSplit(Item(_Frame(), hints={"surface": "content"})))
    panel.render()

    box_line = backend.style_at(2, 0).fg     # a '─' in the box top border
    rule_line = backend.style_at(2, 2).fg    # a '─' in the frame divider
    assert box_line == border                # frame border not lifted
    assert rule_line == border               # divider not lifted (the fix)
    assert rule_line == box_line             # …so the two match


def test_progress_bar_rules_keep_theme_colors_under_auto_ink():
    # The two rules a ProgressBar paints on a character grid — the quiet track and
    # the accent fill — are structural marks, not text. Lifting both to the body
    # floor pushed them to the same near-foreground pale, and the bar then said how
    # far along it was by glyph weight alone; the fill has to stay the accent.
    from puikit.capability import PROFILE_TUI
    from puikit.widgets import ProgressBar

    theme = THEME_TUI
    bg = theme.surfaces["content"]
    # Meaningful only while both colors are ones auto-ink would otherwise lift.
    assert legible_ink(theme.accent, bg, LC_BODY) != theme.accent
    assert legible_ink(theme.control_border, bg, LC_BODY) != theme.control_border

    backend = MemoryBackend(width=20, height=3, capabilities=PROFILE_TUI)
    panel = Panel(backend, theme=theme)
    panel.auto_ink = True
    panel.set_layout(VSplit(Item(ProgressBar(0.5), hints={"surface": "content"})))
    panel.render()

    assert backend.style_at(2, 0).fg == theme.accent           # filled run
    assert backend.style_at(18, 0).fg == theme.control_border  # track


# --- translucent surfaces over a scene ----------------------------------------

from puikit.theme import derive_theme

_PAGE = (3, 22, 26)          # dark water: the page and what a scene is drawn over
_SHELL = (238, 232, 224)     # a white chrome bar
_LIGHT_FG = (207, 230, 234)  # the theme's (light) text colour


class _SceneBackend(MemoryBackend):
    """A compositing memory backend with something drawn behind the UI."""
    has_wallpaper = True


def _shell_theme():
    return derive_theme(background=_PAGE, foreground=_LIGHT_FG, muted=(127, 166, 173),
                        accent=(40, 120, 136), surface=_SHELL, selection=(190, 178, 164),
                        surfaces={"status": _SHELL})


def _render_bar(opacity, wallpaper=True, layer=False):
    cls = _SceneBackend if wallpaper else MemoryBackend
    backend = cls(width=12, height=3, capabilities=PROFILE_GUI_DESKTOP)
    backend.set_surface_opacity(opacity)
    panel = Panel(backend, theme=_shell_theme())
    panel.auto_ink = True
    widget = _Ink(_LIGHT_FG)
    if layer:
        panel.set_layout(VSplit(Item(_Ink(_LIGHT_FG), hints={"surface": "content"})))
        panel.push_layer(widget, hints={"surface": "status", "x": 0, "y": 0, "w": 12, "h": 1})
    else:
        panel.set_layout(VSplit(Item(widget, hints={"surface": "status"})))
    panel.render()
    return backend.style_at(0, 0).fg


def test_text_on_an_opaque_white_bar_is_inked_for_white():
    assert _render_bar(1.0) == legible_ink(_LIGHT_FG, _SHELL, LC_BODY)


def test_text_on_a_translucent_white_bar_is_judged_as_seen():
    # At 0.6 over dark water the white bar is seen as a mid gray: text inked for
    # white came out faint there. It stays dark (the bar's polarity) and goes as
    # dark as the gray needs -- black, since the gray cannot carry body text.
    opaque, seen = _render_bar(1.0), _render_bar(0.6)
    assert sum(seen) < sum(opaque)
    assert seen == (0, 0, 0)


def test_translucency_without_a_scene_changes_nothing():
    assert _render_bar(0.6, wallpaper=False) == _render_bar(1.0, wallpaper=False)


def test_an_opaque_overlay_is_inked_for_its_own_fill():
    # A dialog draws in an opaque group: its fill is what is seen.
    assert _render_bar(0.6, layer=True) == legible_ink(_LIGHT_FG, _SHELL, LC_BODY)


def test_light_text_on_a_translucent_dark_pane_is_unchanged():
    backend = _SceneBackend(width=12, height=3, capabilities=PROFILE_GUI_DESKTOP)
    backend.set_surface_opacity(0.6)
    panel = Panel(backend, theme=_shell_theme())
    panel.auto_ink = True
    panel.set_layout(VSplit(Item(_Ink(_LIGHT_FG), hints={"surface": "content"})))
    panel.render()
    assert backend.style_at(0, 0).fg == _LIGHT_FG


def test_over_fill_inks_against_the_fill_but_does_not_paint_it_again():
    # A widget that filled a row-wide panel (a code block) draws its runs over it:
    # with over_fill the run is still inked for that fill, but a compositing
    # backend gets a transparent bg, so a translucent fill is not doubled behind
    # the glyphs.
    panel_fill = (250, 247, 242)

    class _OverFill(Widget):
        def __init__(self, over):
            self.over = over

        def draw(self, ctx):
            # Drawn on a row this context did not fill, so only the explicit flag
            # (not the automatic detection below) can make it over_fill.
            ctx.draw_text(0, 1, "code", Style(fg=_LIGHT_FG, bg=panel_fill), over_fill=self.over)

    def drawn(over):
        backend = MemoryBackend(width=12, height=3, capabilities=PROFILE_GUI_DESKTOP)
        seen = []
        real = backend.draw_text
        backend.draw_text = lambda x, y, text, style=None: (seen.append(style), real(x, y, text, style))
        panel = Panel(backend, theme=_shell_theme())
        panel.auto_ink = True
        panel.set_layout(VSplit(Item(_OverFill(over), hints={"surface": "content"})))
        panel.render()
        return seen[-1]

    over, plain = drawn(True), drawn(False)
    assert over.bg == TRANSPARENT and plain.bg == panel_fill
    assert over.fg == plain.fg == legible_ink(_LIGHT_FG, panel_fill, LC_BODY)


def test_text_over_a_fill_the_widget_painted_is_over_fill_by_itself():
    # A viewer fills its header bar, then draws the title with the bar's bg: the
    # run is not painted again (no denser box over a translucent bar). The same bg
    # where nothing was filled -- an inline code span -- keeps its own box.
    bar = (240, 235, 228)

    class _Bar(Widget):
        def draw(self, ctx):
            ctx.fill_rect(0, 0, 12, 1, Style(bg=bar))
            ctx.draw_text(1, 0, "title", Style(fg=_LIGHT_FG, bg=bar))
            ctx.draw_text(1, 1, "code", Style(fg=_LIGHT_FG, bg=bar))

    backend = MemoryBackend(width=12, height=3, capabilities=PROFILE_GUI_DESKTOP)
    seen = []
    real = backend.draw_text
    backend.draw_text = lambda x, y, text, style=None: (seen.append((text, style)), real(x, y, text, style))
    panel = Panel(backend, theme=_shell_theme())
    panel.auto_ink = True
    panel.set_layout(VSplit(Item(_Bar(), hints={"surface": "content"})))
    panel.render()
    styles = dict(seen)
    assert styles["title"].bg == TRANSPARENT
    assert styles["code"].bg == bar
    assert styles["title"].fg == styles["code"].fg == legible_ink(_LIGHT_FG, bar, LC_BODY)


def test_surface_opacity_is_seen_by_the_base_layout_but_not_by_an_overlay():
    # A widget that marks its surface (a search hit) reads how much scene shows
    # through: the base layout over a wallpaper sees the backend's opacity, a
    # dialog (opaque group) and a backend with no scene see 1.
    got = {}

    class _Probe(Widget):
        def __init__(self, key):
            self.key = key

        def draw(self, ctx):
            got[self.key] = ctx.surface_opacity

    backend = _SceneBackend(width=12, height=3, capabilities=PROFILE_GUI_DESKTOP)
    backend.set_surface_opacity(0.6)
    panel = Panel(backend, theme=_shell_theme())
    panel.set_layout(VSplit(Item(_Probe("base"), hints={"surface": "content"})))
    panel.push_layer(_Probe("dialog"), hints={"surface": "status", "x": 0, "y": 0, "w": 12, "h": 1})
    panel.render()
    assert got == {"base": 0.6, "dialog": 1.0}
    plain = MemoryBackend(width=12, height=3, capabilities=PROFILE_GUI_DESKTOP)
    plain.set_surface_opacity(0.6)
    panel = Panel(plain, theme=_shell_theme())
    panel.set_layout(VSplit(Item(_Probe("none"), hints={"surface": "content"})))
    panel.render()
    assert got["none"] == 1.0


def test_a_mark_keeps_its_opacity_floor_over_a_scene_only():
    # A selection drawn as a mark (fill_mark): over a scene it keeps MARK_OPACITY
    # instead of fading with the surfaces; in a dialog (opaque group) it is a
    # plain fill.
    from puikit.panel import MARK_OPACITY
    calls = {}

    class _Mark(Widget):
        def __init__(self, key):
            self.key = key

        def draw(self, ctx):
            ctx.fill_mark(0, 0, 4, 1, (166, 78, 62))

    backend = _SceneBackend(width=12, height=3, capabilities=PROFILE_GUI_DESKTOP)
    backend.set_surface_opacity(0.6)
    real = backend.fill_rect

    def record(x, y, w, h, style=None, *, opacity=None):
        if style is not None and style.bg == (166, 78, 62):
            calls.setdefault("seen", []).append(opacity)
        return real(x, y, w, h, style)

    backend.fill_rect = record
    panel = Panel(backend, theme=_shell_theme())
    panel.set_layout(VSplit(Item(_Mark("base"), hints={"surface": "content"})))
    panel.push_layer(_Mark("dialog"), hints={"surface": "status", "x": 0, "y": 2, "w": 12, "h": 1})
    panel.render()
    assert calls["seen"] == [MARK_OPACITY, None]
