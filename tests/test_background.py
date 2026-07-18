"""Background3D descriptor, capability gating, and the pure projection math."""

import math

import pytest

from puikit import Background3D, WIREFRAME, Wallpaper
from puikit.background import (ALPHA_LEVELS, ANIMATIONS, PRESETS, group_by_alpha,
                               wireframe_segments, _CUBE_EDGES)
from puikit.backends.memory_backend import MemoryBackend
from puikit import PROFILE_TUI


# --- the backend-agnostic descriptor -------------------------------------------

def test_defaults():
    b = Background3D()
    assert b.kind == "wireframe"
    assert b.color is None
    assert b.speed == 1.0
    assert 0.0 < b.opacity <= 1.0
    assert not b.is_noop


def test_opacity_is_clamped_to_unit_range():
    assert Background3D(opacity=5.0).opacity == 1.0
    assert Background3D(opacity=-1.0).opacity == 0.0


def test_reveal_is_not_a_scene_property():
    # The surface opacity is a backend-wide, wallpaper-agnostic knob
    # (Backend.set_surface_opacity), not a field of any one scene — so it can be
    # reused across wallpaper kinds and owned by the app's theme.
    import dataclasses
    assert "reveal" not in {f.name for f in dataclasses.fields(Background3D)}


def test_zero_opacity_is_a_noop():
    assert Background3D(opacity=0.0).is_noop
    assert not Background3D(opacity=0.01).is_noop


def test_frozen():
    with pytest.raises(Exception):
        Background3D().speed = 2.0  # type: ignore[misc]


def test_presets():
    assert PRESETS == {"wireframe": WIREFRAME, "cube": WIREFRAME}
    assert WIREFRAME.kind == "wireframe"


def test_animation_registry_has_cube():
    # "cube" and "wireframe" both resolve to the wireframe generator; a backend
    # looks the animation type up here, so a new type is a registry entry.
    assert ANIMATIONS["cube"] is wireframe_segments
    assert ANIMATIONS["wireframe"] is wireframe_segments


def test_wallpaper_descriptor():
    wp = Wallpaper(image="~/pic.png")
    assert wp.fit == "fill" and wp.opacity == 1.0        # defaults
    assert Wallpaper(image="x", opacity=5.0).opacity == 1.0   # clamped
    assert Wallpaper(image="x", opacity=-1.0).opacity == 0.0
    assert Wallpaper(image="").is_noop                   # no path draws nothing
    assert Wallpaper(image="x", opacity=0.0).is_noop     # transparent draws nothing
    assert not Wallpaper(image="x").is_noop


# --- the pure projection math --------------------------------------------------

def test_returns_one_segment_per_cube_edge():
    segs = wireframe_segments(800, 600, 0.0)
    assert len(segs) == len(_CUBE_EDGES) == 12
    for seg in segs:
        assert len(seg) == 4


def test_degenerate_view_yields_nothing():
    assert wireframe_segments(0, 600, 1.0) == []
    assert wireframe_segments(800, 0, 1.0) == []
    assert wireframe_segments(-10, -10, 1.0) == []


def test_all_coordinates_are_finite():
    # The perspective divide can never hit zero (camera distance > cube radius),
    # so no vertex blows up regardless of rotation phase.
    for t in (0.0, 0.37, 1.9, 12.5, 100.0):
        for (x0, y0, x1, y1) in wireframe_segments(640, 480, t, speed=1.3):
            assert all(math.isfinite(v) for v in (x0, y0, x1, y1))


def test_scene_is_centered_and_fits_the_view():
    # The projected cube stays within the view bounds (with a little margin) for
    # any width/height, so it never clips off-window.
    for w, h in ((800, 600), (400, 900), (1600, 300)):
        for t in (0.0, 0.8, 2.1):
            for (x0, y0, x1, y1) in wireframe_segments(w, h, t):
                assert -w * 0.1 <= x0 <= w * 1.1
                assert -h * 0.1 <= y0 <= h * 1.1
                assert -w * 0.1 <= x1 <= w * 1.1
                assert -h * 0.1 <= y1 <= h * 1.1


def test_it_actually_animates():
    # Two distinct times give distinct geometry (the cube really rotates).
    a = wireframe_segments(800, 600, 0.0)
    b = wireframe_segments(800, 600, 0.25)
    assert a != b


def test_speed_zero_is_static():
    a = wireframe_segments(800, 600, 0.0, speed=0.0)
    b = wireframe_segments(800, 600, 5.0, speed=0.0)
    assert a == b


# --- the per-segment alpha contract --------------------------------------------

def test_plain_segments_land_in_the_opaque_bucket():
    # A 4-tuple carries no alpha, so it strokes at the scene's own opacity: one
    # bucket at 1.0 holding every segment (the pre-alpha uniform-stroke behavior).
    segs = [(0.0, 0.0, 1.0, 1.0), (2.0, 2.0, 3.0, 3.0)]
    assert group_by_alpha(segs) == [(1.0, segs)]


def test_the_cube_still_collapses_to_one_stroke():
    grouped = group_by_alpha(wireframe_segments(800, 600, 1.0))
    assert len(grouped) == 1 and grouped[0][0] == 1.0


def test_segments_are_bucketed_by_alpha():
    a = (0.0, 0.0, 1.0, 1.0, 0.5)
    b = (2.0, 2.0, 3.0, 3.0, 0.5)
    c = (4.0, 4.0, 5.0, 5.0, 1.0)
    assert group_by_alpha([a, c, b]) == [(0.5, [a, b]), (1.0, [c])]


def test_buckets_are_ordered_dim_to_bright():
    # Ascending, so a backend paints dim first and bright last — where strokes
    # overlap the brighter one wins rather than being buried.
    grouped = group_by_alpha([
        (0, 0, 1, 1, 0.9), (0, 0, 1, 1, 0.1), (0, 0, 1, 1, 0.5),
    ])
    assert [alpha for alpha, _ in grouped] == sorted(alpha for alpha, _ in grouped)


def test_mixed_forms_coexist_in_one_frame():
    plain = (0.0, 0.0, 1.0, 1.0)
    faded = (2.0, 2.0, 3.0, 3.0, 0.25)
    assert group_by_alpha([plain, faded]) == [(0.25, [faded]), (1.0, [plain])]


def test_transparent_segments_are_dropped():
    # A scene fading a trail to nothing emits alpha 0 rather than special-casing
    # its own tail; those never reach the backend at all.
    assert group_by_alpha([(0, 0, 1, 1, 0.0), (0, 0, 1, 1, -0.5)]) == []


def test_out_of_range_alpha_is_clamped():
    assert group_by_alpha([(0, 0, 1, 1, 5.0)]) == [(1.0, [(0, 0, 1, 1, 5.0)])]


def test_alpha_is_quantized_to_bounded_buckets():
    # A continuously-shaded scene (every star a slightly different alpha) must not
    # cost one stroked path per segment.
    segs = [(0, 0, 1, 1, i / 500.0) for i in range(1, 501)]
    assert len(group_by_alpha(segs)) <= ALPHA_LEVELS


def test_quantization_preserves_every_visible_segment():
    segs = [(0, 0, 1, 1, i / 500.0) for i in range(1, 501)]
    kept = sum(len(group) for _, group in group_by_alpha(segs))
    # Only those quantizing to 0 are dropped; the rest all survive their bucket.
    dropped = sum(1 for s in segs if round(s[4] * ALPHA_LEVELS) == 0)
    assert kept == len(segs) - dropped


# --- capability gating ---------------------------------------------------------

def test_tui_has_no_background_3d():
    assert not PROFILE_TUI.supports("background_3d")


def test_memory_backend_base_set_is_a_safe_noop():
    be = MemoryBackend()
    assert not be.capabilities.supports("background_3d")
    # Inherited base no-op: accepting a call without the capability must not raise.
    be.set_background(WIREFRAME)                  # animation kind
    be.set_background(Wallpaper(image="x.png"))   # wallpaper kind
    be.set_background(None)                        # solid kind
    be.set_surface_opacity(0.6)  # base no-op; a terminal has no sub-cell alpha


# --- macOS wiring (skipped where the backend module is unavailable) ------------

def test_macos_declares_background_3d():
    mb = pytest.importorskip("puikit.backends.macos_backend")
    assert mb.MacOSBackend().capabilities.supports("background_3d")


def test_macos_set_background_dispatches_kinds():
    # set_background accepts all three kinds; has_wallpaper is True for a set
    # animation or image and False for solid. A no-op background clears it.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    be = mb.MacOSBackend()
    assert be.has_wallpaper is False                       # solid by default
    be.set_background(Background3D(kind="cube"))
    assert be.has_wallpaper is True                        # animation
    be.set_background(Wallpaper(image="/some/pic.png"))
    assert be.has_wallpaper is True                        # image
    be.set_background(Wallpaper(image=""))                 # no-op wallpaper
    assert be.has_wallpaper is False                       # cleared
    be.set_background(WIREFRAME)
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
            be.set_background(Background3D(opacity=0.6, backdrop=(0, 0, 0)))
        panel = Panel(be)
        content = panel.theme.surface_bg("content")
        panel.add(Label(""), x=0, y=0, w=10, h=3,
                  hints={"surface": "content", "reveal_mode": "transparent"})
        panel.render()
        return sum(1 for c in be._front if c[0] == "fill" and c[-1].bg == content)

    assert content_fills(wallpaper=False, opacity=0.6) == 1  # no wallpaper: opaque
    assert content_fills(wallpaper=True, opacity=0.6) == 0   # wallpaper: transparent
    assert content_fills(wallpaper=True, opacity=1.0) == 0   # opacity 1 still transparent


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
