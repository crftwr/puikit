"""Tests for the Direct3D 11 shader-background renderer (``_d3d_shader.py``).

The Windows twin of the Metal renderer tests in ``test_shader_background.py``:
they run headless — ``D3DShaderBackground`` renders to an offscreen texture and
reads it back, with no window — and skip where D3D shader support is unavailable
(no ``d3dcompiler``), which is every non-Windows CI runner.

The shaders here are written in HLSL (the dialect this backend compiles), the
counterpart of the MSL used in the Metal tests. The uniforms come from the
``BackgroundUniforms`` cbuffer that ``HLSL_PRELUDE`` declares, referenced as
globals.
"""

import sys

import pytest

from puikit.background import Shader
from puikit.backends._d3d_shader import (
    HAVE_D3D_SHADER,
    HLSL_PRELUDE,
    SHADER_ENTRY,
    UNIFORM_BYTES,
    D3DShaderBackground,
)

pytestmark = pytest.mark.skipif(not HAVE_D3D_SHADER, reason="D3D shader support unavailable")

#: A flat fill from the ``ink`` uniform, to prove uniforms reach the shader.
FLAT_INK = """
float4 puikit_bg_fragment(float4 pos : SV_Position) : SV_Target {
    return float4(ink.rgb, 1.0);
}
"""

#: A horizontal gradient, so the output varies across the frame.
GRADIENT = """
float4 puikit_bg_fragment(float4 pos : SV_Position) : SV_Target {
    float2 uv = pos.xy / resolution;
    return float4(uv.x, uv.y, 0.5, 1.0);
}
"""

#: Encodes the time uniform into the red channel, to prove animation is driven.
TIME_RAMP = """
float4 puikit_bg_fragment(float4 pos : SV_Position) : SV_Target {
    return float4(frac(time), 0.0, 0.0, 1.0);
}
"""

#: A fill from the ``opacity`` uniform, to prove it is passed (advisory) through.
OPACITY_RAMP = """
float4 puikit_bg_fragment(float4 pos : SV_Position) : SV_Target {
    return float4(opacity, opacity, opacity, 1.0);
}
"""


def _pixel(renderer, w, h, elapsed, x=0, y=0):
    """One pixel of a freshly rendered ``w`` x ``h`` frame as (r, g, b, a), 0..255.
    The readback is BGRA, so unswizzle to RGBA."""
    px = renderer.render_pixels(w, h, elapsed)
    if px is None:
        return None
    i = (y * w + x) * 4
    b, g, r, a = px[i], px[i + 1], px[i + 2], px[i + 3]
    return (r, g, b, a)


def _shader(source_hlsl, **kw):
    # A minimal Shader carrying only the HLSL source; ``source`` (MSL) is unused
    # here, so a placeholder keeps the descriptor non-noop for the macOS side.
    return Shader(source="unused", source_hlsl=source_hlsl, **kw)


# --- prelude / entry contract -------------------------------------------------

class TestContract:

    def test_prelude_declares_the_uniform_cbuffer_and_vertex_stage(self):
        assert "BackgroundUniforms" in HLSL_PRELUDE
        assert "puikit_bg_vertex" in HLSL_PRELUDE
        assert "SV_VertexID" in HLSL_PRELUDE

    def test_uniform_bytes_is_16_aligned(self):
        # CreateBuffer requires the constant buffer size be a multiple of 16.
        assert UNIFORM_BYTES % 16 == 0


# --- the renderer -------------------------------------------------------------

class TestRenderer:

    def test_available(self):
        r = D3DShaderBackground()
        assert r.available
        r.close()

    def test_renders_a_texture_of_the_requested_size(self):
        r = D3DShaderBackground()
        assert r.set_shader(_shader(FLAT_INK, ink=(10, 20, 30)))
        px = r.render_pixels(64, 32, 0.0)
        assert px is not None and len(px) == 64 * 32 * 4
        r.close()

    def test_ink_uniform_reaches_the_shader(self):
        r = D3DShaderBackground()
        assert r.set_shader(_shader(FLAT_INK, ink=(200, 100, 50)))
        assert _pixel(r, 8, 8, 0.0)[:3] == (200, 100, 50)
        r.close()

    def test_gradient_varies_across_the_frame(self):
        r = D3DShaderBackground()
        assert r.set_shader(_shader(GRADIENT))
        left = _pixel(r, 16, 4, 0.0, x=0)
        right = _pixel(r, 16, 4, 0.0, x=15)
        assert right[0] > left[0]  # red follows uv.x, so the right edge is brighter
        r.close()

    def test_time_uniform_is_scaled_by_speed_and_drives_animation(self):
        r = D3DShaderBackground()
        assert r.set_shader(_shader(TIME_RAMP))
        first = _pixel(r, 8, 8, 0.0)[0]
        later = _pixel(r, 8, 8, 0.5)[0]
        assert first != later

    def test_speed_zero_freezes_the_scene(self):
        r = D3DShaderBackground()
        assert r.set_shader(_shader(TIME_RAMP, speed=0.0))
        assert _pixel(r, 8, 8, 0.0)[0] == _pixel(r, 8, 8, 9.0)[0]
        r.close()

    def test_opacity_uniform_is_passed_through(self):
        r = D3DShaderBackground()
        assert r.set_shader(_shader(OPACITY_RAMP, opacity=0.5))
        # 0.5 -> ~128 in 8-bit; allow rounding slack.
        assert abs(_pixel(r, 8, 8, 0.0)[0] - 128) <= 2
        r.close()

    def test_resize_between_frames(self):
        r = D3DShaderBackground()
        assert r.set_shader(_shader(FLAT_INK, ink=(10, 20, 30)))
        assert len(r.render_pixels(8, 8, 0.0)) == 8 * 8 * 4
        assert len(r.render_pixels(20, 12, 0.0)) == 20 * 12 * 4
        r.close()

    def test_a_bad_shader_reports_an_error_and_draws_nothing(self):
        r = D3DShaderBackground()
        assert not r.set_shader(_shader("this is not valid HLSL"))
        assert r.error
        assert r.render_to_texture(8, 8, 0.0) is None  # no pixel shader -> no-op
        r.close()

    def test_a_shader_without_hlsl_source_reports_an_error(self):
        # A scene that ships only MSL (source) has no Windows translation.
        r = D3DShaderBackground()
        assert not r.set_shader(Shader(source="msl only"))
        assert r.error and "source_hlsl" in r.error
        r.close()

    def test_entry_point_name_matches_the_shaders(self):
        assert SHADER_ENTRY == "puikit_bg_fragment"


# --- backend capability gate --------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only backend")
class TestBackendGate:

    def test_backend_declares_shader_capability_when_available(self):
        from puikit.backends.windows_backend import WindowsBackend
        b = WindowsBackend()
        assert b.capabilities.supports("background_3d")
        assert b.capabilities.supports("background_shader") == HAVE_D3D_SHADER
