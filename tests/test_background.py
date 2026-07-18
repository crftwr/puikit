"""Background3D descriptor, capability gating, and the pure projection math."""

import math

import pytest

from puikit import Background3D, WIREFRAME
from puikit.background import PRESETS, wireframe_segments, _CUBE_EDGES
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


def test_opacity_and_reveal_are_clamped_to_unit_range():
    assert Background3D(opacity=5.0).opacity == 1.0
    assert Background3D(opacity=-1.0).opacity == 0.0
    assert Background3D(reveal=5.0).reveal == 1.0
    assert Background3D(reveal=-1.0).reveal == 0.0


def test_reveal_defaults_to_opaque_ui():
    # Default 0.0 means "don't touch the UI" — the background only shows through
    # bare surface, so enabling one never changes an existing layout's look.
    assert Background3D().reveal == 0.0


def test_zero_opacity_is_a_noop():
    assert Background3D(opacity=0.0).is_noop
    assert not Background3D(opacity=0.01).is_noop


def test_frozen():
    with pytest.raises(Exception):
        Background3D().speed = 2.0  # type: ignore[misc]


def test_presets():
    assert PRESETS == {"wireframe": WIREFRAME}
    assert WIREFRAME.kind == "wireframe"


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


# --- capability gating ---------------------------------------------------------

def test_tui_has_no_background_3d():
    assert not PROFILE_TUI.supports("background_3d")


def test_memory_backend_base_set_is_a_safe_noop():
    be = MemoryBackend()
    assert not be.capabilities.supports("background_3d")
    # Inherited base no-op: accepting a call without the capability must not raise.
    be.set_background_3d(WIREFRAME)
    be.set_background_3d(None)


# --- macOS wiring (skipped where the backend module is unavailable) ------------

def test_macos_declares_background_3d():
    mb = pytest.importorskip("puikit.backends.macos_backend")
    assert mb.MacOSBackend().capabilities.supports("background_3d")


def test_macos_ui_fill_alpha_tracks_reveal():
    # The surface-fill opacity used by _render_fill: 1.0 (opaque) with no
    # background or reveal=0, and 1 - reveal while one asks to show through.
    # Exercised without a window (no open()), so it stays a headless unit test.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    be = mb.MacOSBackend()
    assert be._ui_fill_alpha() == 1.0                 # no background
    be._background_3d = Background3D(reveal=0.0)
    assert be._ui_fill_alpha() == 1.0                 # opaque UI by default
    be._background_3d = Background3D(reveal=0.4)
    assert be._ui_fill_alpha() == pytest.approx(0.6)  # panes go translucent


def test_macos_reveal_exempt_group_stays_opaque():
    # Inside a reveal-exempt (opaque) overlay group, surface fills ignore the
    # active reveal so the layer occludes the base UI instead of dissolving it.
    mb = pytest.importorskip("puikit.backends.macos_backend")
    be = mb.MacOSBackend()
    be._background_3d = Background3D(reveal=0.4)
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
