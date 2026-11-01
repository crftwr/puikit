"""Shader textures and the background lens: the descriptors, and the Metal path.

The Direct3D twins of the renderer tests live in ``test_d3d_shader.py``. Like
the rest of the shader tests these run headless — ``render_to_texture`` draws to
an offscreen texture — and skip where Metal is unavailable.
"""

import pytest

from puikit.background import Lens, LensCycle, Shader, ShaderTexture, slot_hash
from puikit.backends import _bg_lens
from puikit.backends._metal import HAVE_METAL, MetalBackground

PIL = pytest.importorskip("PIL.Image")

metal_only = pytest.mark.skipif(not HAVE_METAL, reason="Metal unavailable")

#: Samples texture 0 at the frame's centre and shows it.
SAMPLE_MSL = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]],
                                   texture2d<float> t [[texture(0)]],
                                   sampler s [[sampler(0)]]) {
    return float4(t.sample(s, float2(0.5, 0.5), level(0)).rgb, 1.0);
}
"""

#: Linear HDR: a tiny, very bright disc on a dim field — what a lens is for.
SUN_MSL = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]]) {
    float2 c = pos.xy - u.resolution * 0.5;
    return float4(float3(length(c) < 2.0 ? 400.0 : 0.05), 1.0);
}
"""

_BACKDROP = (10, 20, 30)


def _png(tmp_path, name, rgb):
    path = tmp_path / name
    PIL.new("RGB", (8, 8), rgb).save(path)
    return str(path)


def _rgb(px, width, x, y):
    i = (y * width + x) * 4
    return (px[i + 2], px[i + 1], px[i])


def _frame(renderer, width=200, height=120, elapsed=1.0, fade=1.0):
    return MetalBackground.texture_pixels(
        renderer.render_to_texture(width, height, elapsed, fade))


class TestDescriptors:

    def test_a_path_is_shorthand_for_a_texture(self):
        s = Shader(source="x", textures=["~/a.png"])
        assert s.textures == (ShaderTexture("~/a.png"),)

    def test_texture_defaults_suit_a_world_map(self):
        t = ShaderTexture("a.png")
        assert (t.srgb, t.wrap_u, t.wrap_v, t.mipmaps) == (False, "repeat", "clamp", True)

    def test_an_unknown_wrap_mode_clamps(self):
        assert ShaderTexture("a.png", wrap_u="mirror").wrap_u == "clamp"

    def test_lens_tints_and_matrix_are_normalized(self):
        lens = Lens(mono_tint=(0.5, 1), color_matrix=[1, 2, 3])
        assert lens.mono_tint == (0.5, 1.0, 1.0)
        assert lens.matrix_rows == ((1.0, 2.0, 3.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        assert Lens().matrix_rows[1] == (0.0, 1.0, 0.0)

    def test_a_zero_threshold_cannot_bloom_black(self):
        assert Lens(threshold=0.0).threshold > 0.0

    def test_uniform_blocks_match_their_declared_sizes(self):
        lens = Lens(streak=1.0)
        block = _bg_lens.lens_uniforms(lens, 100, 50, 1.0, 1.0, (0, 0, 0, 1), True)
        assert len(block) == _bg_lens.LENS_UNIFORM_BYTES
        assert len(_bg_lens.down_uniforms((8, 8), (4, 4), True, 1.0)) == \
            _bg_lens.PASS_UNIFORM_BYTES

    def test_the_web_backend_declines_a_textured_or_lensed_scene(self):
        from puikit.backends.web_backend import WebBackend
        backend = WebBackend.__new__(WebBackend)
        backend._background = Shader(source="x", source_glsl="vec4 f;", lens=Lens())
        assert backend._shader_program() is None
        backend._background = Shader(source="x", source_glsl="vec4 f;")
        assert backend._shader_program() is not None


@metal_only
class TestMetalTextures:

    def test_a_texture_is_sampled(self, tmp_path):
        r = MetalBackground()
        assert r.set_shader(Shader(source=SAMPLE_MSL,
                                   textures=[_png(tmp_path, "red.png", (200, 0, 0))]))
        assert _rgb(_frame(r, 16, 16), 16, 8, 8) == (200, 0, 0)

    def test_an_srgb_texture_samples_linear(self, tmp_path):
        r = MetalBackground()
        tex = ShaderTexture(_png(tmp_path, "grey.png", (128, 128, 128)), srgb=True)
        assert r.set_shader(Shader(source=SAMPLE_MSL, textures=[tex]))
        # sRGB 128 is linear 0.216; written straight to an 8-bit target, ~55.
        assert abs(_rgb(_frame(r, 16, 16), 16, 8, 8)[0] - 55) <= 2

    def test_a_missing_texture_leaves_the_backdrop(self, tmp_path):
        r = MetalBackground()
        assert r.set_shader(Shader(source=SAMPLE_MSL, backdrop=_BACKDROP,
                                   textures=[str(tmp_path / "missing.png")]))
        px = _frame(r, 16, 16)
        assert {_rgb(px, 16, x, y) for x in range(16) for y in range(16)} == {_BACKDROP}
        assert "missing.png" in r.error

    def test_a_new_texture_set_replaces_the_old(self, tmp_path):
        r = MetalBackground()
        assert r.set_shader(Shader(source=SAMPLE_MSL,
                                   textures=[_png(tmp_path, "r.png", (200, 0, 0))]))
        _frame(r, 16, 16)
        assert r.set_shader(Shader(source=SAMPLE_MSL,
                                   textures=[_png(tmp_path, "g.png", (0, 200, 0))]))
        assert _rgb(_frame(r, 16, 16), 16, 8, 8) == (0, 200, 0)


@metal_only
class TestMetalLens:

    def _render(self, lens, fade=1.0):
        r = MetalBackground()
        assert r.set_shader(Shader(source=SUN_MSL, lens=lens, backdrop=_BACKDROP)), r.error
        return _frame(r, fade=fade)

    def test_hdr_is_tone_mapped_not_clipped_flat(self):
        px = self._render(Lens(bloom=0.0, vignette=0.0))
        # The 400x sun saturates to white; the 0.05 field maps to a dim grey well
        # above black — HDR in, display colour out.
        assert _rgb(px, 200, 100, 60) == (255, 255, 255)
        field = _rgb(px, 200, 10, 10)
        assert 30 < field[0] < 90

    def test_bloom_spreads_the_sun(self):
        plain = self._render(Lens(bloom=0.0, vignette=0.0))
        bloomed = self._render(Lens(bloom=1.0, vignette=0.0))
        assert _rgb(bloomed, 200, 115, 60)[0] > _rgb(plain, 200, 115, 60)[0] + 5

    def test_every_feature_draws_without_nan(self):
        # NaN reaches an 8-bit target as black; a frame with every feature on must
        # still show the bright sun and a lit field near it.
        px = self._render(Lens(streak=1, ghosts=1, scanlines=1, grain=0.5, distortion=0.2,
                               aberration=0.01, halation=1, color_matrix=(0.1, 1.3, -0.5,
                                                                          1, 0, 0, 0, 0, 1)))
        assert sum(_rgb(px, 200, 100, 60)) > 600
        assert sum(_rgb(px, 200, 120, 60)) > 0

    def test_masks_reach_the_backdrop(self):
        # Letterbox bars and the scope mask black the image out, and the image is
        # then mixed over the backdrop — so the corners show black, not backdrop,
        # at full opacity...
        px = self._render(Lens(scope=1.0))
        assert _rgb(px, 200, 2, 2) == (0, 0, 0)

    def test_zero_fade_is_the_bare_backdrop(self):
        px = self._render(Lens(), fade=0.0)
        assert {_rgb(px, 200, x, y) for x in range(0, 200, 7) for y in range(0, 120, 7)} \
            == {_BACKDROP}


class TestCompatibility:
    """The additive recipe (CLAUDE.md): the new fields change nothing for old callers."""

    def test_old_positional_arity_still_binds(self):
        s = Shader("src", 0.6, 0.7, (1, 2, 3), (4, 5, 6), 0.5, "hlsl", "glsl")
        assert (s.source_glsl, s.textures, s.lens) == ("glsl", (), None)

    def test_asdict_round_trips_with_textures_and_lens(self):
        import dataclasses
        s = Shader(source="x", textures=[ShaderTexture("a.png", srgb=True)],
                   lens=Lens(grain=0.2, color_matrix=(1, 0, 0, 0, 1, 0, 0, 0, 1)))
        assert Shader(**dataclasses.asdict(s)) == s


#: Writes puikit_slot_hash(slot = pixel column, seed) into the colour channels.
HASH_MSL = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]]) {
    uint h = puikit_slot_hash(int(pos.x), u.seed);
    return float4(float(h & 255u), float((h >> 8) & 255u), float((h >> 24) & 255u), 255.0)
         / 255.0;
}
"""


class TestSeedAndCycle:

    @metal_only
    def test_the_gpu_hash_matches_python(self):
        r = MetalBackground()
        assert r.set_shader(Shader(source=HASH_MSL)), r.error
        for seed in (0.0, 1.0, 40321.0, 65535.0):
            px = MetalBackground.texture_pixels(r.render_to_texture(64, 1, 0.0, seed=seed))
            for x in range(64):
                h = slot_hash(x, seed)
                assert _rgb(px, 64, x, 0) == (h & 255, (h >> 8) & 255, (h >> 24) & 255)

    def test_the_hash_spreads(self):
        picks = {slot_hash(k, 123) % 10 for k in range(60)}
        assert len(picks) == 10

    def test_a_cycle_holds_a_look_then_blends_to_the_next(self):
        looks = (Lens(bloom=0.0), Lens(bloom=1.0), Lens(bloom=0.5))
        cycle = LensCycle(looks, period=40.0, transition=10.0)
        seed = next(s for s in range(100)
                    if slot_hash(0, s) % 3 != slot_hash(1, s) % 3)
        a, b = looks[slot_hash(0, seed) % 3], looks[slot_hash(1, seed) % 3]
        assert cycle.at(5.0, seed) == a
        assert cycle.at(29.9, seed) == a
        mid = cycle.at(35.0, seed).bloom
        assert min(a.bloom, b.bloom) < mid < max(a.bloom, b.bloom)
        assert cycle.at(40.0, seed) == b

    def test_the_seed_changes_the_sequence(self):
        cycle = LensCycle(tuple(Lens(bloom=i / 10) for i in range(10)))
        runs = {tuple(cycle.at(k * 40 + 1, seed).bloom for k in range(8)) for seed in (1, 2, 3)}
        assert len(runs) == 3

    @metal_only
    def test_a_cycle_drives_the_lens_pass(self):
        cycle = LensCycle((Lens(bloom=0.0, vignette=0.0), Lens(saturation=0.0, gain=3.0)),
                          period=10.0, transition=2.0)
        r = MetalBackground()
        assert r.set_shader(Shader(source=SUN_MSL, lens=cycle, backdrop=_BACKDROP)), r.error
        frames = {bytes(MetalBackground.texture_pixels(r.render_to_texture(64, 40, t, seed=s)))
                  for s in (1.0, 2.0, 3.0, 4.0) for t in (1.0, 11.0, 21.0)}
        assert len(frames) > 1


class TestLevelAndSeedBlend:

    @metal_only
    def test_level_caps_the_brightest_pixel(self):
        r = MetalBackground()
        assert r.set_shader(Shader(source=SUN_MSL, lens=Lens(bloom=1.0, level=0.5),
                                   backdrop=(0, 0, 0))), r.error
        px = _frame(r)
        assert max(max(px[i], px[i + 1], px[i + 2]) for i in range(0, len(px), 4)) <= 128

    def test_level_is_clamped(self):
        assert Lens(level=2.0).level == 1.0 and Lens(level=-1).level == 0.0

    def test_a_cycle_blends_between_seeds(self):
        from puikit.backends._bg_lens import current_lens
        cycle = LensCycle((Lens(bloom=0.0), Lens(bloom=1.0)), period=40.0, transition=1.0)
        a, b = next((x, y) for x in range(50) for y in range(50)
                    if cycle.at(5.0, x).bloom == 0.0 and cycle.at(5.0, y).bloom == 1.0)
        assert current_lens(cycle, 5.0, b, a, 0.0).bloom == 0.0
        assert 0.0 < current_lens(cycle, 5.0, b, a, 0.5).bloom < 1.0
        assert current_lens(cycle, 5.0, b, a, 1.0).bloom == 1.0
