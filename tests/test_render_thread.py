"""The render thread's hand-over (puikit/backends/_render_thread.py): requests
coalesce, the frame holds the lock, a failing frame does not end the thread.
Platform-independent; the Windows backend's use of it is covered in
test_windows_backend.py."""

import threading
import time

from puikit.backends._render_thread import ENV, RenderThread, pipeline_wanted


def test_pipeline_wanted_follows_the_environment(monkeypatch):
    assert pipeline_wanted({ENV: "1"}) is True
    assert pipeline_wanted({ENV: "on"}) is True
    assert pipeline_wanted({ENV: "0"}) is False
    assert pipeline_wanted({ENV: "off"}) is False
    # Unset or auto: only where the GIL is off.
    monkeypatch.setattr("puikit.backends._render_thread.free_threaded", lambda: True)
    assert pipeline_wanted({}) is True
    assert pipeline_wanted({ENV: "auto"}) is True
    monkeypatch.setattr("puikit.backends._render_thread.free_threaded", lambda: False)
    assert pipeline_wanted({}) is False


def test_a_request_draws_a_frame_on_another_thread():
    seen = []
    thread = RenderThread(lambda: seen.append(threading.get_ident()))
    try:
        thread.request()
        assert thread.wait_idle()
        assert seen == [thread.ident]
        assert seen[0] != threading.get_ident()
    finally:
        thread.stop()


def test_requests_during_a_frame_coalesce_into_one_more():
    started, release = threading.Event(), threading.Event()
    frames = []

    def render():
        frames.append(time.monotonic())
        if len(frames) == 1:
            started.set()
            release.wait(5)

    thread = RenderThread(render)
    try:
        thread.request()
        assert started.wait(5)
        for _ in range(10):         # a burst while the first frame draws
            thread.request()
        release.set()
        assert thread.wait_idle()
        assert len(frames) == 2     # the first, and one for the whole burst
        assert thread.coalesced == 9
    finally:
        thread.stop()


def test_the_frame_holds_the_lock():
    inside, release = threading.Event(), threading.Event()

    def render():
        inside.set()
        release.wait(5)

    thread = RenderThread(render)
    try:
        thread.request()
        assert inside.wait(5)
        assert not thread.lock.acquire(timeout=0.05)   # held by the frame
        release.set()
        assert thread.lock.acquire(timeout=5)
        thread.lock.release()
    finally:
        thread.stop()


def test_the_lock_is_reentrant_for_a_frame_that_calls_back():
    thread = RenderThread(lambda: None)
    done = []

    def render():
        with thread.lock:           # e.g. an image delivered during the frame
            done.append(True)

    thread._render = render
    try:
        thread.request()
        assert thread.wait_idle()
        assert done == [True]
    finally:
        thread.stop()


def test_a_failing_frame_leaves_the_thread_drawing(caplog):
    calls = []

    def render():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    thread = RenderThread(render)
    try:
        thread.request()
        assert thread.wait_idle()
        thread.request()
        assert thread.wait_idle()
        assert len(calls) == 2
        assert "render thread frame failed" in caplog.text
    finally:
        thread.stop()


def test_stop_ends_the_thread_and_ignores_later_requests():
    calls = []
    thread = RenderThread(lambda: calls.append(1))
    thread.stop()
    assert not thread._thread.is_alive()
    thread.request()
    time.sleep(0.05)
    assert calls == []
