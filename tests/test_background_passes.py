"""Shader textures, targets and passes: the descriptors, the graph, and the Metal path.

The Direct3D twins of the renderer tests live in ``test_d3d_shader.py``. Like
the rest of the shader tests these run headless — ``render_to_texture`` draws to
an offscreen texture — and skip where Metal is unavailable.
"""

import dataclasses
import struct

import pytest

from puikit.background import MAX_PASS_INPUTS, Pass, Shader, ShaderTexture, Target
from puikit.backends import _bg_graph
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

#: Three passes: a left/right split into a half-size target, a pass that
#: scales it by its params into another, and the screen showing that, with the
#: sizes each pass was told written into green and blue.
CHAIN_MSL = """
fragment float4 split(float4 pos [[position]], constant BackgroundUniforms &u [[buffer(0)]],
                      constant PassUniforms &p [[buffer(1)]]) {
    return float4(pos.x < p.target.x * 0.5 ? 0.25 : 0.75, p.target.x / 255.0, 0.0, 1.0);
}
fragment float4 scale(float4 pos [[position]], constant BackgroundUniforms &u [[buffer(0)]],
                      constant PassUniforms &p [[buffer(1)]],
                      texture2d<float> src [[texture(0)]], sampler s [[sampler(0)]]) {
    float4 c = src.sample(s, pos.xy / p.target.xy, level(0));
    return float4(c.r * p.params.x, c.g, p.inputs[0].x / 255.0, 1.0);
}
fragment float4 show(float4 pos [[position]], constant BackgroundUniforms &u [[buffer(0)]],
                     constant PassUniforms &p [[buffer(1)]],
                     texture2d<float> src [[texture(0)]], sampler s [[sampler(0)]]) {
    return src.sample(s, pos.xy / p.target.xy, level(0));
}
"""

CHAIN = dict(
    source=CHAIN_MSL,
    targets=(Target("half", scale=0.5), Target("scaled", scale=0.5, format="rgba8")),
    passes=(Pass("split", "half"),
            Pass("scale", "scaled", inputs=("half",), params=(2.0,)),
            Pass("show", inputs=("scaled",))))

#: A once pass baking texel coordinates into a 3D target, and the screen
#: sampling it.
VOLUME_MSL = """
fragment float4 bake(float4 pos [[position]], constant BackgroundUniforms &u [[buffer(0)]],
                     constant PassUniforms &p [[buffer(1)]]) {
    return float4(pos.x / p.target.x, pos.y / p.target.y, (p.target.w + 0.5) / p.target.z, 1.0);
}
fragment float4 show(float4 pos [[position]], constant BackgroundUniforms &u [[buffer(0)]],
                     constant PassUniforms &p [[buffer(1)]],
                     texture3d<float> v [[texture(0)]], sampler s [[sampler(0)]]) {
    float2 uv = pos.xy / u.resolution;
    return float4(v.sample(s, float3(uv, uv.x), level(0)).rgb, 1.0);
}
"""


def _volume(depth=4):
    return dict(source=VOLUME_MSL, targets=(Target("vol", size=(16, 8, depth)),),
                passes=(Pass("bake", "vol", once=True), Pass("show", inputs=("vol",))))


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

    def test_a_target_is_sized_from_the_drawable_or_absolutely(self):
        assert Target("a", scale=0.5).extent(101, 51) == (50, 25, 1)
        assert Target("a", scale=0.001).extent(100, 100) == (1, 1, 1)
        assert Target("a", size=(7, 9)).extent(100, 100) == (7, 9, 1)
        assert Target("a", size=(7, 9, 3)).is_3d
        assert not Target("a", size=(7, 9)).is_3d

    def test_target_sizes_and_formats_are_clamped(self):
        assert Target("a", size=(0, 9999, 9999)).size == (1, 4096, 512)
        assert Target("a", format="rgba32f").format == "rgba16f"

    def test_pass_params_and_inputs_are_normalized(self):
        p = Pass("e", "", inputs="a", params=(1, 2))
        assert (p.output, p.inputs, p.params) == (None, ("a",), (1.0, 2.0, 0.0, 0.0))

    def test_frame_rate_is_never_negative(self):
        assert Shader(source="x", frame_rate=-3).frame_rate == 0.0

    def test_asdict_round_trips(self):
        s = Shader(source="x", textures=[ShaderTexture("a.png", name="a")], frame_rate=30,
                   **{k: v for k, v in CHAIN.items() if k != "source"})
        assert Shader(**dataclasses.asdict(s)) == s

    def test_the_web_backend_declines_a_textured_or_multi_pass_scene(self):
        from puikit.backends.web_backend import WebBackend
        backend = WebBackend.__new__(WebBackend)
        backend._background = Shader(source_glsl="vec4 f;", **CHAIN)
        assert backend._shader_program() is None
        backend._background = Shader(source="x", source_glsl="vec4 f;", textures=["a.png"])
        assert backend._shader_program() is None
        backend._background = Shader(source="x", source_glsl="vec4 f;")
        assert backend._shader_program() is not None


class TestPlan:

    def test_no_passes_is_the_single_pass_form(self):
        plan = _bg_graph.plan(Shader(source="x", textures=["a.png", "b.png"]))
        assert plan.error is None and plan.once == ()
        (step,) = plan.frame
        assert (step.entry, step.output) == ("puikit_bg_fragment", None)
        assert step.inputs == (("texture", 0), ("texture", 1))

    def test_names_resolve_to_slots(self):
        plan = _bg_graph.plan(Shader(textures=[ShaderTexture("a.png", name="map")],
                                     **{**CHAIN, "passes": (
                                         Pass("split", "half", inputs=("map",)),
                                         *CHAIN["passes"][1:])}))
        assert plan.error is None
        assert plan.frame[0].inputs == (("texture", 0),)
        assert plan.frame[1].inputs == (("target", 0),) and plan.frame[1].output == 1
        assert plan.entries == {("split", "rgba16f"), ("scale", "rgba8"), ("show", None)}

    @pytest.mark.parametrize("targets, passes, why", [
        ((), (Pass("a", "t"),), "draw to the screen"),
        ((Target("t"),), (Pass("a"), Pass("b")), "only the last"),
        ((Target("t"),), (Pass("a", "nope"), Pass("b")), "no target named"),
        ((Target("t"),), (Pass("a", "t", inputs=("t",)), Pass("b")), "its own output"),
        ((Target("t"),), (Pass("a", inputs=("t",)),), "before any pass writes"),
        ((Target("t"), Target("t")), (Pass("a"),), "repeats"),
        ((Target("t"),), (Pass("a", "t", once=True), Pass("b")), "absolute size"),
        ((Target("t", size=(4, 4)), Target("f")),
         (Pass("f", "f"), Pass("a", "t", inputs=("f",), once=True), Pass("b")),
         "before any pass writes"),
        ((Target("t", size=(4, 4)),),
         (Pass("a", "t", once=True), Pass("c", "t"), Pass("b")), "once pass baked"),
        ((Target("t"),), (Pass("a", inputs=("t",) * (MAX_PASS_INPUTS + 1)),), "inputs"),
        ((Target("t"),), (), "targets without passes"),
    ])
    def test_a_graph_that_does_not_hold_together_is_refused(self, targets, passes, why):
        plan = _bg_graph.plan(Shader(source="x", targets=targets, passes=passes))
        assert why in (plan.error or "") and plan.frame == ()

    def test_a_frame_pass_may_read_a_bake(self):
        assert _bg_graph.plan(Shader(**_volume())).error is None

    def test_the_bake_runs_once_per_slice(self):
        work = _bg_graph.once_work(_bg_graph.plan(Shader(**_volume(depth=5))))
        assert [z for _step, z in work] == [0, 1, 2, 3, 4]

    def test_pass_uniforms_pack_params_target_and_inputs(self):
        step = _bg_graph.Step("e", 0, (("target", 1),), (1.0, 2.0, 3.0, 4.0), False)
        data = _bg_graph.pass_uniforms(step, (10, 20, 3), 2, [(5, 6, 1)])
        assert len(data) == _bg_graph.PASS_UNIFORM_BYTES
        values = struct.unpack(f"{len(data) // 4}f", data)
        assert values[:12] == (1.0, 2.0, 3.0, 4.0, 10.0, 20.0, 3.0, 2.0, 5.0, 6.0, 1.0, 0.0)
        assert set(values[12:]) == {0.0}


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

    def test_a_pass_reads_a_texture_by_name(self, tmp_path):
        r = MetalBackground()
        assert r.set_shader(Shader(
            source=SAMPLE_MSL.replace("puikit_bg_fragment", "show"),
            textures=[ShaderTexture(_png(tmp_path, "b.png", (0, 0, 200)), name="blue")],
            passes=[Pass("show", inputs=("blue",))])), r.error
        assert _rgb(_frame(r, 16, 16), 16, 8, 8) == (0, 0, 200)


@metal_only
class TestMetalPasses:

    def test_passes_run_in_order_through_their_targets(self):
        r = MetalBackground()
        assert r.set_shader(Shader(**CHAIN)), r.error
        px = _frame(r, 100, 40)
        # split: 0.25 | 0.75; scale doubles it, clamped in the rgba8 target.
        assert abs(_rgb(px, 100, 10, 20)[0] - 128) <= 2
        assert _rgb(px, 100, 90, 20)[0] == 255
        # Each pass was told its sizes: split's target and scale's input are
        # half the drawable.
        assert _rgb(px, 100, 10, 20)[1] == 50
        assert _rgb(px, 100, 10, 20)[2] == 50

    def test_targets_follow_the_drawable(self):
        r = MetalBackground()
        assert r.set_shader(Shader(**CHAIN))
        _frame(r, 100, 40)
        assert _rgb(_frame(r, 60, 40), 60, 10, 20)[1] == 30

    def test_a_broken_graph_draws_nothing_and_says_why(self):
        r = MetalBackground()
        assert not r.set_shader(Shader(source=CHAIN_MSL, passes=[Pass("show", inputs=("x",))]))
        assert "no texture or target named 'x'" in r.error
        assert r.render_to_texture(8, 8, 0.0) is None

    def test_a_missing_entry_is_named(self):
        r = MetalBackground()
        assert not r.set_shader(Shader(source=CHAIN_MSL, passes=[Pass("nope")]))
        assert "nope" in r.error

    def test_the_screen_pass_still_gets_the_backdrop_clear(self):
        r = MetalBackground()
        assert r.set_shader(Shader(**CHAIN, backdrop=_BACKDROP))
        assert r._plan.frame[-1].output is None

    def test_reapplying_the_same_graph_keeps_its_pipelines(self):
        r = MetalBackground()
        assert r.set_shader(Shader(**CHAIN))
        pipes = dict(r._pipes)
        assert r.set_shader(Shader(**CHAIN, speed=2.0))
        assert r._pipes == pipes


@metal_only
class TestMetalBake:

    def test_a_3d_target_is_baked_and_sampled(self):
        r = MetalBackground()
        assert r.set_shader(Shader(**_volume())), r.error
        px = MetalBackground.texture_pixels(r.render_to_texture(32, 32, 0.0))
        # The texel coordinates come back: the corners differ, the middle is mid-grey.
        assert abs(_rgb(px, 32, 16, 16)[0] - 128) < 12
        assert _rgb(px, 32, 1, 1)[0] < 40 and _rgb(px, 32, 30, 30)[0] > 200

    def test_the_bake_is_spread_over_frames_showing_the_backdrop(self):
        import Metal
        from puikit.backends._metal import PIXEL_FORMAT
        r = MetalBackground()
        depth = _bg_graph.BAKE_SLICES_PER_FRAME * 3
        assert r.set_shader(Shader(**_volume(depth), backdrop=_BACKDROP))
        d = Metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
            PIXEL_FORMAT, 8, 8, False)
        d.setUsage_(Metal.MTLTextureUsageRenderTarget | Metal.MTLTextureUsageShaderRead)
        tex = r._device.newTextureWithDescriptor_(d)
        frames = []
        for _ in range(4):
            c = r._encode(tex, 8, 8, 0.0)
            c.commit()
            c.waitUntilCompleted()
            frames.append(_rgb(MetalBackground.texture_pixels(tex), 8, 0, 0))
        # Two frames of backdrop; the third bakes the last slices and draws.
        assert frames[:2] == [_BACKDROP] * 2 and frames[2] != _BACKDROP

    def test_reapplying_the_same_shader_does_not_rebake(self):
        r = MetalBackground()
        assert r.set_shader(Shader(**_volume()))
        r.render_to_texture(8, 8, 0.0)
        assert r.set_shader(Shader(**_volume(), speed=2.0))
        assert r._bake_next == len(r._bake_work) > 0


@metal_only
class TestFrameRate:

    def test_a_capped_scene_skips_frames_inside_its_interval(self):
        r = MetalBackground()
        assert r.set_shader(Shader(source=SAMPLE_MSL, frame_rate=10.0))
        assert r._too_soon(1.0) is False          # the first frame always goes
        assert r._too_soon(1.0) is True           # the next, a moment later, waits
        assert r._too_soon(0.5) is False          # unless the scene is fading

    def test_an_uncapped_scene_never_skips(self):
        r = MetalBackground()
        assert r.set_shader(Shader(source=SAMPLE_MSL))
        assert not r._too_soon(1.0) and not r._too_soon(1.0)
