"""Background descriptors and the capability gating around set_background.

The shader kind's own compile/draw behaviour lives in test_shader_background.py
(Metal) and test_d3d_shader.py (HLSL); this file covers the descriptors and the
backend wiring they ride through.
"""

import pytest

from puikit import Shader, Wallpaper

#: A minimal well-formed shader, for tests that just need *an* animated
#: background. Never compiled here — these cover descriptors and wiring.
_SHADER = Shader(source="fragment float4 puikit_bg_fragment() { return 0; }")
from puikit.backends.memory_backend import MemoryBackend
from puikit import PROFILE_TUI


# --- the backend-agnostic descriptor -------------------------------------------

def test_reveal_is_not_a_scene_property():
    # The surface opacity is a backend-wide, background-agnostic knob
    # (Backend.set_surface_opacity), not a field of any one scene — so it can be
    # reused across background kinds and owned by the app's theme.
    import dataclasses
    for kind in (Shader, Wallpaper):
        assert "reveal" not in {f.name for f in dataclasses.fields(kind)}


def test_wallpaper_descriptor():
    wp = Wallpaper(image="~/pic.png")
    assert wp.fit == "fill" and wp.opacity == 1.0        # defaults
    assert Wallpaper(image="x", opacity=5.0).opacity == 1.0   # clamped
    assert Wallpaper(image="x", opacity=-1.0).opacity == 0.0
    assert Wallpaper(image="").is_noop                   # no path draws nothing
    assert Wallpaper(image="x", opacity=0.0).is_noop     # transparent draws nothing
    assert not Wallpaper(image="x").is_noop


# --- capability gating ---------------------------------------------------------

def test_tui_has_no_background():
    assert not PROFILE_TUI.supports("background")


def test_memory_backend_base_set_is_a_safe_noop():
    be = MemoryBackend()
    assert not be.capabilities.supports("background")
    # Inherited base no-op: accepting a call without the capability must not raise.
    be.set_background(_SHADER)                     # shader kind
    be.set_background(Wallpaper(image="x.png"))   # wallpaper kind
    be.set_background(None)                        # solid kind
    be.set_surface_opacity(0.6)  # base no-op; a terminal has no sub-cell alpha


# --- macOS wiring (skipped where the backend module is unavailable) ------------

def test_macos_declares_background():
    mb = pytest.importorskip("puikit.backends.macos_backend")
    assert mb.MacOSBackend().capabilities.supports("background")


def test_macos_set_background_dispatches_kinds():
    # set_background accepts all three kinds; has_wallpaper is True for a set
    # shader or image and False for solid. A no-op background clears it.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    be = mb.MacOSBackend()
    assert be.has_wallpaper is False                       # solid by default
    be.set_background(_SHADER)
    assert be.has_wallpaper is True                        # shader
    be.set_background(Wallpaper(image="/some/pic.png"))
    assert be.has_wallpaper is True                        # image
    be.set_background(Wallpaper(image=""))                 # no-op wallpaper
    assert be.has_wallpaper is False                       # cleared
    be.set_background(_SHADER)
    assert be.has_wallpaper is True


class _Pt:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _Size:
    def __init__(self, width, height):
        self.width, self.height = width, height


class _Rect:
    def __init__(self, x, y, w, h):
        self.origin, self.size = _Pt(x, y), _Size(w, h)


def test_macos_wallpaper_rect_fit_modes():
    # The destination rect for each fit, into a 200x100 window with a 100x100 image.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    bounds, size = _Rect(0, 0, 200, 100), _Size(100, 100)

    def rect(fit):
        r = mb._wallpaper_rect(bounds, size, fit)
        return (round(r.origin.x, 3), round(r.origin.y, 3),
                round(r.size.width, 3), round(r.size.height, 3))

    assert rect("stretch") == (0, 0, 200, 100)            # exact fill, aspect ignored
    assert rect("fill") == (0, -50, 200, 200)             # cover: scaled to width, cropped
    assert rect("fit") == (50, 0, 100, 100)               # contain: scaled to height, centered
    assert rect("center") == (50, 0, 100, 100)            # native, centered


def test_macos_ui_fill_alpha_tracks_surface_opacity():
    # The surface-fill opacity used by _render_fill: 1.0 (opaque) by default, and
    # the set surface opacity once lowered (independent of any wallpaper). Exercised
    # without a window (no open()), so it stays a headless unit test.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    be = mb.MacOSBackend()
    assert be._ui_fill_alpha() == 1.0                 # opaque UI by default
    be.set_surface_opacity(1.0)
    assert be._ui_fill_alpha() == 1.0                 # still opaque
    be.set_surface_opacity(0.6)
    assert be._ui_fill_alpha() == pytest.approx(0.6)  # panes go translucent
    be.set_surface_opacity(-1.0)                      # clamped to 0.0
    assert be._ui_fill_alpha() == pytest.approx(0.0)


def test_macos_reveal_exempt_group_stays_opaque():
    # Inside a reveal-exempt (opaque) overlay group, surface fills ignore the
    # surface opacity so the layer occludes the base UI instead of dissolving it.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    be = mb.MacOSBackend()
    be.set_surface_opacity(0.6)
    assert be._ui_fill_alpha() == pytest.approx(0.6)  # base pane: dissolves
    be._reveal_exempt_depth = 1
    assert be._ui_fill_alpha() == 1.0                 # overlay layer: opaque
    be._reveal_exempt_depth = 0
    assert be._ui_fill_alpha() == pytest.approx(0.6)  # back to dissolving


def test_macos_begin_group_records_opaque_flag():
    # The Panel marks overlay-layer groups opaque; base slots leave it False. The
    # flag rides the group_begin command so the render pass can scope the reveal.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    be = mb.MacOSBackend()
    base_key, overlay_key = object(), object()
    be.begin_group(base_key)
    be.begin_group(overlay_key, rect=None, opaque=True)
    assert be._back[0] == ("group_begin", id(base_key), None, False)
    assert be._back[1] == ("group_begin", id(overlay_key), None, True)


def test_macos_transparent_slot_skips_fill_only_over_wallpaper():
    # A reveal_mode="transparent" slot (the file/log panes) drops its surface fill
    # whenever a wallpaper is present, so it shows at full strength — gated on the
    # wallpaper existing, NOT on the surface opacity, so it stays transparent even at
    # opacity 1. With no wallpaper (a plain theme) it fills opaquely like any pane.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    from puikit import Panel
    from puikit.widgets import Label

    def content_fills(*, wallpaper: bool, opacity: float) -> int:
        be = mb.MacOSBackend()  # compositing backend: supports transparency
        be.set_surface_opacity(opacity)
        if wallpaper:
            be.set_background(Shader(source=_SHADER.source, opacity=0.6,
                                     backdrop=(0, 0, 0)))
        panel = Panel(be)
        content = panel.theme.surface_bg("content")
        panel.add(Label(""), x=0, y=0, w=10, h=3,
                  hints={"surface": "content", "reveal_mode": "transparent"})
        panel.render()
        return sum(1 for c in be._front if c[0] == "fill" and c[-1].bg == content)

    assert content_fills(wallpaper=False, opacity=0.6) == 1  # no wallpaper: opaque
    assert content_fills(wallpaper=True, opacity=0.6) == 0   # wallpaper: transparent
    assert content_fills(wallpaper=True, opacity=1.0) == 0   # opacity 1 still transparent


def test_draw_context_wallpaper_gates_a_self_painted_page_fill():
    # The self_paint counterpart of reveal_mode="transparent": a widget that paints
    # its own full-window page (a modal viewer) asks ctx.wallpaper whether to drop
    # it, so the scene shows at full strength rather than through one more surface.
    # Gated on the wallpaper existing, not on the surface opacity — and always False
    # on a grid backend, which has nothing to show through.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    from puikit import Panel
    from puikit.widgets import Widget

    class _Probe(Widget):
        seen = None

        def draw(self, ctx):
            _Probe.seen = ctx.wallpaper

    def probe(backend, *, wallpaper: bool, opacity: float = 1.0) -> bool:
        backend.set_surface_opacity(opacity)
        if wallpaper:
            backend.set_background(Shader(source=_SHADER.source, opacity=0.6,
                                          backdrop=(0, 0, 0)))
        panel = Panel(backend)
        panel.add(_Probe(), x=0, y=0, w=10, h=3)
        panel.render()
        return _Probe.seen

    assert probe(mb.MacOSBackend(), wallpaper=False) is False
    assert probe(mb.MacOSBackend(), wallpaper=True) is True
    assert probe(mb.MacOSBackend(), wallpaper=True, opacity=1.0) is True
    # No compositing: the widget keeps filling opaquely even when asked for a
    # background (a grid backend has no "background" capability, so the set is a
    # no-op and there is nothing to reveal).
    assert probe(MemoryBackend(20, 6), wallpaper=True) is False


def test_macos_nested_same_surface_fill_is_deduped():
    # A pane nested in a same-surface parent must not fill that surface twice: under
    # a reveal the double-blend would dim the animated scene more there than under a
    # singly-filled neighbour (the file panes vs the bare log). draw_child skips the
    # redundant fill on a compositing backend; a *different* surface still fills.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    from puikit import Panel
    from puikit.widgets import Label

    def content_fill_count(child_surface: str) -> int:
        be = mb.MacOSBackend()  # compositing backend: supports transparency
        panel = Panel(be)
        content = panel.theme.surface_bg("content")

        class Nest(Label):
            def draw(self, ctx):
                ctx.draw_child(Label(""), 0, 0, ctx.size_units[0], 1.0,
                               hints={"surface": child_surface})

        # The parent slot fills "content"; the nested child then claims a surface.
        panel.add(Nest(""), x=0, y=0, w=10, h=3, hints={"surface": "content"})
        panel.render()
        return sum(1 for c in be._front if c[0] == "fill" and c[-1].bg == content)

    # Child re-claims "content" → only the parent slot's fill survives (deduped).
    assert content_fill_count("content") == 1
    # Child claims a different surface → its own fill stays; the one "content" fill
    # is still just the parent's.
    assert content_fill_count("header") == 1
