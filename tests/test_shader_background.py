"""The Shader background kind: the descriptor, the uniform contract, and the
Metal renderer.

The renderer tests run headless — MetalBackground can draw to an offscreen texture
as readily as to a layer, so the GPU path is exercised and its output inspected
without ever opening a window. They skip where Metal is unavailable.
"""

import dataclasses

import pytest

from puikit import Shader
from puikit.background import SHADER_ENTRY, SHADER_PRELUDE
from puikit.backends._metal import HAVE_METAL, MetalBackground, UNIFORM_BYTES
from puikit.backends.memory_backend import MemoryBackend
from puikit import PROFILE_TUI

pytestmark_metal = pytest.mark.skipif(not HAVE_METAL, reason="Metal unavailable")

#: A shader that paints the ink color everywhere — the simplest way to prove a
#: uniform reached the GPU, since the output pixel *is* the uniform.
FLAT_INK = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]]) {
    return float4(u.ink.rgb, 1.0);
}
"""

#: Paints a horizontal gradient, so the output varies across the frame.
GRADIENT = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]]) {
    float2 uv = pos.xy / u.resolution;
    return float4(uv.x, uv.y, 0.5, 1.0);
}
"""

#: Encodes the time uniform into the red channel, to prove animation is driven.
TIME_RAMP = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]]) {
    return float4(fract(u.time), 0.0, 0.0, 1.0);
}
"""


def _pixel(texture, x=0, y=0):
    """One pixel as (r, g, b, a), 0..255. The texture is BGRA, so unswizzle."""
    px = MetalBackground.texture_pixels(texture)
    w = int(texture.width())
    i = (y * w + x) * 4
    b, g, r, a = px[i], px[i + 1], px[i + 2], px[i + 3]
    return (r, g, b, a)


# --- the descriptor ------------------------------------------------------------

class TestDescriptor:

    def test_defaults(self):
        s = Shader(source="x")
        assert s.speed == 1.0 and s.opacity == 1.0
        assert s.ink is None and s.backdrop is None
        assert not s.is_noop

    def test_opacity_is_clamped(self):
        assert Shader(source="x", opacity=5.0).opacity == 1.0
        assert Shader(source="x", opacity=-1.0).opacity == 0.0

    def test_is_noop(self):
        # No source and full transparency both mean "draws nothing", so the
        # backend can drop the background without inspecting the shader.
        assert Shader(source="").is_noop
        assert Shader(source="x", opacity=0.0).is_noop
        assert not Shader(source="x", opacity=0.01).is_noop

    def test_frozen(self):
        with pytest.raises(Exception):
            Shader(source="x").speed = 2.0  # type: ignore[misc]

    def test_program_prepends_the_prelude(self):
        # The app writes only a fragment function; the uniform struct and vertex
        # stage come from the prelude, so an app shader cannot break them.
        s = Shader(source=FLAT_INK)
        assert s.program.startswith(SHADER_PRELUDE)
        assert FLAT_INK in s.program
        assert "puikit_bg_vertex" in s.program
        assert "BackgroundUniforms" in s.program

    def test_it_is_a_distinct_background_kind(self):
        # Not a Background3D subclass: the backend dispatches on type, and a
        # shader takes an entirely different (GPU) path.
        from puikit import Background3D
        assert not isinstance(Shader(source="x"), Background3D)
        assert {f.name for f in dataclasses.fields(Shader)} == {
            "source", "speed", "opacity", "ink", "backdrop", "resolution_scale"}

    def test_resolution_scale_is_clamped_above_zero(self):
        # Zero would ask for a zero-sized drawable, which is an error rather than
        # a cheap frame, so the floor is deliberately not 0.
        assert Shader(source="x", resolution_scale=0.0).resolution_scale == 0.1
        assert Shader(source="x", resolution_scale=-3).resolution_scale == 0.1
        assert Shader(source="x", resolution_scale=9).resolution_scale == 1.0
        assert Shader(source="x").resolution_scale == 1.0


# --- capability gating ---------------------------------------------------------

class TestCapability:

    def test_tui_does_not_support_it(self):
        assert not PROFILE_TUI.supports("background_shader")

    def test_unknown_capability_defaults_to_unsupported(self):
        # Backends that never heard of the capability report False rather than
        # raising, which is what lets this ship without touching every profile.
        assert not MemoryBackend().capabilities.supports("background_shader")

    def test_setting_one_without_the_capability_is_a_safe_noop(self):
        MemoryBackend().set_background(Shader(source=FLAT_INK))

    def test_macos_declares_it_when_metal_is_present(self):
        mb = pytest.importorskip("puikit.backends.macos_backend")
        supported = mb.MacOSBackend().capabilities.supports("background_shader")
        assert supported == (HAVE_METAL and mb.CAMetalLayer is not None)


class TestFrameRate:
    """A shader must drive the frame timer at the animation rate.

    It is the one animated thing that does *not* repaint the UI, which makes it
    easy to leave out of the "is anything animating?" test — and the symptom is
    subtle: it still animates, just at the 10Hz idle-poll rate. Per-frame cost
    looks identical either way, so only the achieved rate reveals it.
    """

    def _backend(self, background):
        mb = pytest.importorskip("puikit.backends.macos_backend")
        be = mb.MacOSBackend()
        be._background = background
        return be

    def _wants_fast(self, be):
        from puikit.backends.macos_backend import Background3D as B3D
        return (bool(be._animations) or be._roll_active()
                or isinstance(be._background, (B3D, Shader)))

    def test_a_shader_asks_for_the_animation_rate(self):
        assert self._wants_fast(self._backend(Shader(source=FLAT_INK)))

    def test_an_animation_still_asks_for_the_animation_rate(self):
        from puikit import Background3D
        assert self._wants_fast(self._backend(Background3D(kind="cube")))

    def test_a_static_background_does_not(self):
        # A wallpaper never changes and no background at all needs nothing, so
        # neither should hold the timer at 60Hz.
        from puikit import Wallpaper
        assert not self._wants_fast(self._backend(None))
        assert not self._wants_fast(self._backend(Wallpaper(image="x.png")))

    def test_the_two_rates_are_what_they_claim(self):
        mb = pytest.importorskip("puikit.backends.macos_backend")
        assert mb.MacOSBackend._ANIM_INTERVAL == pytest.approx(1 / 60.0)
        assert mb.MacOSBackend._IDLE_TICK_INTERVAL == pytest.approx(1 / 10.0)


# --- the Metal renderer --------------------------------------------------------

@pytestmark_metal
class TestRenderer:

    def test_uniform_buffer_is_16_byte_aligned(self):
        # The struct ends in two float4s, which Metal aligns to 16 bytes; the
        # packing in _metal.py and the struct in the prelude must agree.
        assert UNIFORM_BYTES % 16 == 0

    def test_compiles_and_draws(self):
        r = MetalBackground()
        assert r.available
        assert r.set_shader(Shader(source=GRADIENT))
        assert r.error is None
        tex = r.render_to_texture(64, 32, 0.0)
        assert tex is not None
        px = MetalBackground.texture_pixels(tex)
        assert len({bytes(px[i:i + 4]) for i in range(0, len(px), 4)}) > 2

    def test_ink_uniform_reaches_the_shader(self):
        # The whole point of the uniform block: a scene stays on-palette because
        # the theme's colors arrive as uniforms. Painting ink flat lets us read
        # the uniform straight back out of the framebuffer.
        r = MetalBackground()
        assert r.set_shader(Shader(source=FLAT_INK, ink=(200, 224, 245)))
        rgb = _pixel(r.render_to_texture(8, 8, 0.0))[:3]
        assert all(abs(a - b) <= 1 for a, b in zip(rgb, (200, 224, 245))), rgb

    def test_time_uniform_advances(self):
        r = MetalBackground()
        assert r.set_shader(Shader(source=TIME_RAMP))
        first = _pixel(r.render_to_texture(8, 8, 0.0))[0]
        later = _pixel(r.render_to_texture(8, 8, 0.5))[0]
        assert first != later

    def test_speed_scales_time(self):
        # speed multiplies the time uniform, so speed=0 freezes the scene — the
        # same contract the segment scenes honour.
        frozen = MetalBackground()
        assert frozen.set_shader(Shader(source=TIME_RAMP, speed=0.0))
        assert (_pixel(frozen.render_to_texture(8, 8, 0.0))[0]
                == _pixel(frozen.render_to_texture(8, 8, 9.0))[0])

    def test_backdrop_is_the_clear_color(self):
        # A shader that discards nothing still needs a defined clear, and it must
        # be the theme backdrop so a light theme does not flash dark.
        r = MetalBackground()
        transparent = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]]) {
    return float4(u.backdrop.rgb, 1.0);
}
"""
        assert r.set_shader(Shader(source=transparent, backdrop=(16, 30, 50)))
        rgb = _pixel(r.render_to_texture(8, 8, 0.0))[:3]
        assert all(abs(a - b) <= 1 for a, b in zip(rgb, (16, 30, 50))), rgb

    def test_bad_source_fails_without_raising(self):
        # A shader with a typo must cost a blank background and an error string,
        # not a crash in the middle of a theme switch.
        r = MetalBackground()
        assert not r.set_shader(Shader(source="this is not MSL"))
        assert r.error and "error" in r.error.lower()
        assert r.render_to_texture(8, 8, 0.0) is None

    def test_source_missing_the_entry_point_is_rejected(self):
        r = MetalBackground()
        ok = r.set_shader(Shader(source="""
fragment float4 wrongly_named(float4 pos [[position]],
                              constant BackgroundUniforms &u [[buffer(0)]]) {
    return u.ink;
}
"""))
        assert not ok
        assert SHADER_ENTRY in (r.error or "")

    def test_recompiles_only_when_the_source_changes(self):
        # A theme switch that keeps the same shader must not pay the Metal
        # compiler again; only new source does.
        r = MetalBackground()
        assert r.set_shader(Shader(source=FLAT_INK, ink=(10, 20, 30)))
        first = r._pipeline
        assert r.set_shader(Shader(source=FLAT_INK, ink=(200, 100, 50)))
        assert r._pipeline is first          # same source: pipeline reused
        assert _pixel(r.render_to_texture(8, 8, 0.0))[:3] == (200, 100, 50)
        assert r.set_shader(Shader(source=GRADIENT))
        assert r._pipeline is not first      # new source: recompiled

    def test_render_without_a_shader_is_a_noop(self):
        assert MetalBackground().render_to_texture(8, 8, 0.0) is None
