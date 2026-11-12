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
    def __init__(self, infos):
        self._infos = infos

    def domain(self):
        return "MTLCommandBufferErrorDomain"

    def code(self):
        return 2

    def localizedDescription(self):
        return "Caused GPU Hang Error"

    def userInfo(self):
        return {_metal.Metal.MTLCommandBufferEncoderInfoErrorKey: self._infos}


class _Command:
    def __init__(self, ok=True, gpu_s=0.010, infos=()):
        self._ok, self._gpu, self._infos = ok, gpu_s, list(infos)

    def GPUStartTime(self):
        return 1.0

    def GPUEndTime(self):
        return 1.0 + self._gpu

    def status(self):
        return (_metal.Metal.MTLCommandBufferStatusCompleted if self._ok
                else _metal.Metal.MTLCommandBufferStatusError)

    def error(self):
        return None if self._ok else _Error(self._infos)

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
    assert len(reports) == _metal.GPU_REPORT_LIMIT
    assert "not reported" in reports[-1][-1]


def test_a_report_survives_an_unreadable_error():
    class Broken(_Command):
        def error(self):
            raise_me = object()
            return raise_me          # no domain()/userInfo(): must not raise
    reports = []
    GpuWatch(sink=reports.append).completed(Broken(ok=False), {})
    assert "not readable" in "\n".join(reports[0])


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
