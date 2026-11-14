"""The frame statistics line (puikit/backends/_frame_stats.py, PUIKIT_FPS).
Platform-independent: the GPU timer needs a D3D11 device and is exercised on
Windows only through the backend."""

from puikit.backends._frame_stats import ENV, FrameStats, wanted


def test_wanted_follows_the_environment():
    assert wanted({ENV: "1"}) is True
    assert wanted({ENV: "0"}) is False
    assert wanted({}) is False


def test_the_line_counts_frames_new_lists_and_ui_passes(caplog):
    caplog.set_level("INFO", logger="puikit.backends._frame_stats")
    stats = FrameStats(pipeline=True)
    a, b = [], []
    stats.ui_begin()
    stats.ui_end()
    stats.frame(a, 20.0, [], present_ms=0.5, inflight=1)
    stats.frame(a, 22.0, [])        # the same list again: an animation frame
    assert "measuring" in stats.text
    stats._since -= 10.0            # the second's window closes on the next frame
    stats.frame(b, 24.0, [5.0], present_ms=0.7, inflight=2)
    text = stats.text
    assert text.startswith("draw ")
    assert "render 22.0ms" in text  # median of 20, 22, 24
    assert "present 0.6ms max 0.7" in text
    assert "inflight 1.50 max 2" in text
    assert "gpu 5.0ms" in text
    assert text.endswith(("pipe gil", "pipe nogil"))
    assert text in caplog.text
    # 3 frames, 2 lists: draw counts every frame, new only the second list's.
    draw = float(text.split()[1].rstrip("/s"))
    new = float(text.split()[3])
    assert draw > new > 0


def test_no_gpu_time_shows_why():
    stats = FrameStats(pipeline=False)
    stats._since -= 10.0
    stats.frame([], 10.0, [], "(disjoint 3)")
    assert "gpu - (disjoint 3)" in stats.text
    assert "| seq " in stats.text
