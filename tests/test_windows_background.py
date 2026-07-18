"""Tests for the WindowsBackend animated-background integration (set_background,
surface opacity, the reveal exemption, and the wallpaper geometry helper).

Mirrors the philosophy of test_windows_backend.py: these run without opening a
window — they exercise the descriptor plumbing and pure helpers, not pixels (the
GPU shader path is covered end-to-end in test_d3d_shader.py, and the segment
projection math in test_background.py)."""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only backend")
pytest.importorskip("ctypes.wintypes", reason="Windows-only backend")

from puikit.background import Background3D, Shader, Wallpaper  # noqa: E402
from puikit.backends.windows_backend import (  # noqa: E402
    WindowsBackend, _approach, _smoothstep, _wallpaper_dest,
)


# --- pure helpers -------------------------------------------------------------

class TestWallpaperDest:

    def test_stretch_fills_exactly(self):
        assert _wallpaper_dest(100, 50, 40, 40, "stretch") == (0.0, 0.0, 100, 50)

    def test_center_keeps_native_size_and_centers(self):
        x, y, w, h = _wallpaper_dest(100, 50, 40, 20, "center")
        assert (w, h) == (40, 20)
        assert (x, y) == ((100 - 40) / 2, (50 - 20) / 2)

    def test_fit_contains_letterboxed(self):
        # A 40x40 image into 100x50: min(100/40, 50/40)=1.25 -> 50x50, centered.
        x, y, w, h = _wallpaper_dest(100, 50, 40, 40, "fit")
        assert (w, h) == (50, 50)
        assert x == (100 - 50) / 2 and y == (50 - 50) / 2

    def test_fill_covers_overflowing(self):
        # A 40x20 image into 100x50: max(100/40, 50/20)=2.5 -> 100x50 exactly here,
        # but a squarer image overflows one axis (centered, cropped by the target).
        x, y, w, h = _wallpaper_dest(100, 50, 40, 40, "fill")
        assert w == 100 and h == 100  # cover scales to the wider axis; overflows height
        assert y < 0  # centered, so it hangs off top and bottom equally

    def test_degenerate_image_falls_back_to_stretch(self):
        assert _wallpaper_dest(100, 50, 0, 0, "fill") == (0.0, 0.0, 100, 50)


class TestRateHelpers:

    def test_smoothstep_endpoints_and_midpoint(self):
        assert _smoothstep(0.0) == 0.0
        assert _smoothstep(1.0) == 1.0
        assert _smoothstep(0.5) == pytest.approx(0.5)

    def test_smoothstep_clamps(self):
        assert _smoothstep(-1.0) == 0.0
        assert _smoothstep(2.0) == 1.0

    def test_approach_rises_toward_target(self):
        assert _approach(0.0, 1.0, 1.0, up=2.0, down=4.0) == pytest.approx(0.5)

    def test_approach_falls_slower_with_a_longer_span(self):
        assert _approach(1.0, 0.0, 1.0, up=2.0, down=4.0) == pytest.approx(0.75)

    def test_approach_zero_span_snaps(self):
        assert _approach(0.0, 1.0, 0.1, up=0.0, down=4.0) == 1.0


# --- descriptor plumbing (no window) ------------------------------------------

class TestBackgroundApi:

    def test_no_background_by_default(self):
        b = WindowsBackend()
        assert b.has_wallpaper is False
        assert b._background is None

    def test_set_background3d_registers_and_reports_wallpaper(self):
        b = WindowsBackend()
        b.set_background(Background3D(kind="wireframe"))
        assert b.has_wallpaper is True
        assert isinstance(b._background, Background3D)
        assert b._bg_running  # a ticker was armed

    def test_set_wallpaper_reports_wallpaper_but_no_tick(self):
        b = WindowsBackend()
        b.set_background(Wallpaper(image="nope.png"))
        assert b.has_wallpaper is True
        assert not b._bg_running  # a static image needs no animation tick

    def test_a_noop_background_is_dropped(self):
        b = WindowsBackend()
        b.set_background(Background3D(kind="wireframe", opacity=0.0))
        assert b._background is None
        assert b.has_wallpaper is False

    def test_clearing_the_background(self):
        b = WindowsBackend()
        b.set_background(Background3D(kind="wireframe"))
        b.set_background(None)
        assert b._background is None
        assert b.has_wallpaper is False

    def test_setting_a_background_resets_its_clock(self):
        b = WindowsBackend()
        b.set_background(Background3D(kind="wireframe"))
        b._bg_clock = 5.0
        b.set_background(Background3D(kind="wireframe", speed=2.0))
        assert b._bg_clock == 0.0 and b._bg_rate == 1.0


class TestSurfaceOpacity:

    def test_default_is_opaque(self):
        b = WindowsBackend()
        assert b.surface_opacity == 1.0
        assert b._ui_fill_alpha() == 1.0

    def test_set_and_clamp(self):
        b = WindowsBackend()
        b.set_surface_opacity(0.3)
        assert b.surface_opacity == 0.3 and b._ui_fill_alpha() == 0.3
        b.set_surface_opacity(5.0)
        assert b.surface_opacity == 1.0
        b.set_surface_opacity(-1.0)
        assert b.surface_opacity == 0.0

    def test_reveal_exempt_group_stays_opaque(self):
        b = WindowsBackend()
        b.set_surface_opacity(0.2)
        b._reveal_exempt_depth = 1
        assert b._ui_fill_alpha() == 1.0  # an overlay occludes rather than dissolves
        b._reveal_exempt_depth = 0
        assert b._ui_fill_alpha() == 0.2


class TestBackgroundRate:

    def test_target_is_zero_when_window_inactive(self):
        b = WindowsBackend()
        b._window_active = False
        assert b._bg_target(0.0) == 0.0

    def test_target_full_while_active_and_recent_input(self):
        import time
        b = WindowsBackend()
        b._window_active = True
        b._last_input_time = time.monotonic()
        assert b._bg_target(time.monotonic()) == 1.0

    def test_target_zero_after_idle_timeout(self):
        b = WindowsBackend()
        b._window_active = True
        b._last_input_time = 0.0
        assert b._bg_target(10_000.0) == 0.0
