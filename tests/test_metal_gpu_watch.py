"""GpuWatch: every command buffer reports its own failure (xefm#892).

A real GPU fault cannot be provoked safely on a development Mac (macOS starts
refusing the process's submissions), so the failure report is driven with a
stand-in command buffer; a real render checks the handler is attached and
records GPU times.
"""

import time

import pytest

from puikit.backends import _metal
from puikit.backends._metal import HAVE_METAL, GpuWatch, MetalBackground

pytestmark = pytest.mark.skipif(not HAVE_METAL, reason="Metal unavailable")


class _Info:
    def __init__(self, label, state):
        self._label, self._state = label, state

    def label(self):
        return self._label

    def errorState(self):
        return self._state

    def debugSignposts(self):
        return []


class _Error:
    def __init__(self, infos, code=2):
        self._infos, self._code = infos, code

    def domain(self):
        return "MTLCommandBufferErrorDomain"

    def code(self):
        return self._code

    def localizedDescription(self):
        return "Caused GPU Hang Error"

    def userInfo(self):
        return {_metal.Metal.MTLCommandBufferEncoderInfoErrorKey: self._infos}


class _Command:
    def __init__(self, ok=True, gpu_s=0.010, infos=(), code=2):
        self._ok, self._gpu, self._infos = ok, gpu_s, list(infos)
        self._code = code

    def GPUStartTime(self):
        return 1.0

    def GPUEndTime(self):
        return 1.0 + self._gpu

    def status(self):
        return (_metal.Metal.MTLCommandBufferStatusCompleted if self._ok
                else _metal.Metal.MTLCommandBufferStatusError)

    def error(self):
        return None if self._ok else _Error(self._infos, self._code)

    def label(self):
        return "frame"


def test_a_failed_buffer_names_the_faulted_pass_and_the_frames_before():
    reports = []
    watch = GpuWatch(sink=reports.append)
    for ms in (10, 11, 12):
        watch.completed(_Command(gpu_s=ms / 1000.0), {})
    watch.completed(_Command(ok=False, infos=[_Info("pelagic -> scene", 4),
                                              _Info("lens_down -> down0", 3)]),
                    {"time": 1090.5, "size": "1360x884"})
    assert watch.failures == 1
    text = "\n".join(reports[0])
    assert "Caused GPU Hang Error" in text and "code 2" in text
    assert "pass 'pelagic -> scene': FAULTED" in text
    assert "pass 'lens_down -> down0': pending" in text
    assert "time=1090.50" in text and "size=1360x884" in text
    assert "10.0 11.0 12.0" in text


def test_only_the_first_failures_are_reported():
    reports = []
    watch = GpuWatch(sink=reports.append)
    for _ in range(_metal.GPU_REPORT_LIMIT + 5):
        watch.completed(_Command(ok=False), {})
    assert watch.failures == _metal.GPU_REPORT_LIMIT + 5
    failures = [r for r in reports if "failed #" in r[0]]
    assert len(failures) == _metal.GPU_REPORT_LIMIT
    assert "not reported" in failures[-1][-1]


def test_a_report_survives_an_unreadable_error():
    class Broken(_Command):
        def error(self):
            raise_me = object()
            return raise_me          # no domain()/userInfo(): must not raise
    reports = []
    GpuWatch(sink=reports.append).completed(Broken(ok=False), {})
    assert "not readable" in "\n".join(reports[0])


def test_an_aborted_buffer_has_no_gpu_time():
    reports = []
    watch = GpuWatch(sink=reports.append)
    watch.gpu, watch.scene = "Apple M3 Pro", "pelagic"
    watch.completed(_Command(ok=False, gpu_s=6147620589.3887), {})
    text = "\n".join(reports[0])
    assert "GPU ms of this buffer: n/a (aborted)" in text
    assert "scene: pelagic, GPU: Apple M3 Pro" in text


@pytest.mark.parametrize("code", sorted(_metal._STOP_CODES))
def test_a_failure_not_to_repeat_stops_the_scene_once(code):
    reports, stops = [], []
    watch = GpuWatch(sink=reports.append)
    watch.on_stop = lambda: stops.append(1)
    watch.completed(_Command(), {})
    assert watch.stopped is None
    watch.completed(_Command(ok=False, code=code), {})
    watch.completed(_Command(ok=False, code=code), {})
    assert watch.stopped and stops == [1]
    assert sum("stopped after a GPU failure" in r[0] for r in reports) == 1


def test_a_lone_victim_does_not_stop_the_scene_but_a_run_does():
    watch = GpuWatch(sink=lambda lines: None)
    for _ in range(4):
        watch.completed(_Command(ok=False, code=1), {})
        watch.completed(_Command(ok=False, code=1), {})
        watch.completed(_Command(), {})          # drew again: the run is broken
    assert watch.stopped is None
    for _ in range(_metal._STOP_AFTER):
        watch.completed(_Command(ok=False, code=1), {})
    assert watch.stopped


def test_a_new_scene_starts_clean_and_ignores_the_old_ones_buffers():
    stops = []
    watch = GpuWatch(sink=lambda lines: None)
    watch.on_stop = lambda: stops.append(1)
    old = watch._generation
    watch.completed(_Command(ok=False), {}, old)
    assert watch.stopped
    watch.reset("crystal")
    assert watch.stopped is None
    watch.completed(_Command(ok=False), {}, old)     # was still in flight
    assert watch.stopped is None and stops == [1]
    watch.completed(_Command(ok=False), {}, watch._generation)
    assert watch.stopped


_PLAIN = """
fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                   constant BackgroundUniforms &u [[buffer(0)]]) {
    return float4(%s, 0.0, 0.0, 1.0);
}"""


def test_the_same_scene_stays_stopped_and_another_one_draws():
    from puikit.background import Shader
    r = MetalBackground()
    r.gpu_watch._sink = lambda lines: None
    assert r.set_shader(Shader(source=_PLAIN % "1.0", label="one")), r.error
    assert r.gpu_watch.scene == "one" and r.gpu_watch.gpu
    r.gpu_watch.completed(_Command(ok=False), {})

    class _Layer:
        def nextDrawable(self):
            raise AssertionError("a stopped scene asked for a drawable")
    assert r.render_to_layer(_Layer(), 0.0) is False
    assert r.set_shader(Shader(source=_PLAIN % "1.0", label="one", opacity=0.5))
    assert r.gpu_watch.stopped
    assert r.set_shader(Shader(source=_PLAIN % "0.5", label="two")), r.error
    assert r.gpu_watch.stopped is None and r.gpu_watch.scene == "two"


class _FakeLayer:
    removed = False

    def removeFromSuperlayer(self):
        self.removed = True


def test_the_backend_drops_the_layer_and_parks(monkeypatch):
    mb = pytest.importorskip("puikit.backends.macos_backend")
    from puikit.background import Shader
    be = mb.MacOSBackend()
    posted = []
    monkeypatch.setattr(be, "call_on_main_thread", posted.append)
    monkeypatch.setattr(be, "request_animation_ticks", lambda cb: None)
    be.set_background(Shader(source=_PLAIN % "1.0", label="one"))
    be._metal.gpu_watch._sink = lambda lines: None
    be._metal_layer = layer = _FakeLayer()
    assert be._bg_running
    be._metal.gpu_watch.completed(_Command(ok=False), {})
    assert be._background_tick() is False and not be._bg_running
    assert not layer.removed            # only the main thread touches the layer
    for callback in posted:
        callback()
    assert layer.removed and be._metal_layer is None
    be._ensure_background_ticker()      # input does not wake a stopped scene
    assert not be._bg_running
    be.set_background(Shader(source=_PLAIN % "0.5", label="two"))
    assert be._bg_running


def test_a_stop_overtaken_by_a_new_scene_keeps_its_layer(monkeypatch):
    mb = pytest.importorskip("puikit.backends.macos_backend")
    from puikit.background import Shader
    be = mb.MacOSBackend()
    posted = []
    monkeypatch.setattr(be, "call_on_main_thread", posted.append)
    monkeypatch.setattr(be, "request_animation_ticks", lambda cb: None)
    be.set_background(Shader(source=_PLAIN % "1.0"))
    be._metal.gpu_watch._sink = lambda lines: None
    be._metal.gpu_watch.completed(_Command(ok=False), {})
    be.set_background(Shader(source=_PLAIN % "0.5"))
    be._metal_layer = layer = _FakeLayer()
    posted[0]()
    assert not layer.removed and be._metal_layer is layer


def test_a_real_frame_goes_through_the_watch():
    from puikit.background import Shader
    from puikit.background import SHADER_PRELUDE  # noqa: F401 - prelude is implicit
    r = MetalBackground()
    src = """
    fragment float4 puikit_bg_fragment(float4 pos [[position]],
                                       constant BackgroundUniforms &u [[buffer(0)]]) {
        return float4(1.0, 0.0, 0.0, 1.0);
    }"""
    assert r.set_shader(Shader(source=src)), r.error
    assert r.render_to_texture(16, 16, 0.0) is not None
    deadline = time.monotonic() + 2.0
    while not r.gpu_watch._history and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(r.gpu_watch._history) == 1
    assert r.gpu_watch.failures == 0
