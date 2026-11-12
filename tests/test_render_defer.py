"""Panel.render puts a frame off while input waits in the queue (xefm held-key overrun).

Run with: python -m pytest tests/test_render_defer.py -v
"""

import time
import unittest

from puikit import Panel
from puikit.backends.memory_backend import MemoryBackend


class _Backend(MemoryBackend):
    def __init__(self):
        super().__init__(width=40, height=10)
        self.pending = False
        self.timers = []

    def input_pending(self):
        return self.pending

    def call_later(self, delay_seconds, callback):
        self.timers.append(callback)
        return lambda: None


class RenderDefer(unittest.TestCase):

    def setUp(self):
        self.b = _Backend()
        self.b.open()
        self.panel = Panel(self.b)
        self.frames = 0
        impl = self.panel._render_impl

        def counted():
            self.frames += 1
            impl()
        self.panel._render_impl = counted

    def tearDown(self):
        self.b.close()

    def test_renders_when_nothing_waits(self):
        self.panel.render()
        self.panel.render()
        self.assertEqual(self.frames, 2)
        self.assertEqual(self.b.timers, [])

    def test_first_frame_after_a_pause_is_never_put_off(self):
        self.b.pending = True
        self.panel.render()
        self.assertEqual(self.frames, 1)

    def test_burst_renders_once_when_the_queue_drains(self):
        self.panel.render()
        self.b.pending = True
        for _ in range(5):
            self.panel.render()
        self.assertEqual(self.frames, 1)
        self.assertEqual(len(self.b.timers), 1, "one timer for the whole burst")
        self.b.pending = False
        self.b.timers.pop()()
        self.assertEqual(self.frames, 2)

    def test_owed_timer_does_nothing_once_a_frame_was_drawn(self):
        self.panel.render()
        self.b.pending = True
        self.panel.render()
        self.b.pending = False
        self.panel.render()           # drawn directly, pays the debt
        self.b.timers.pop()()
        self.assertEqual(self.frames, 2)

    def test_long_burst_still_shows_progress(self):
        self.panel.render()
        self.b.pending = True
        self.panel.render()
        self.assertEqual(self.frames, 1)
        self.panel._last_frame_time = time.monotonic() - Panel.DEFER_MAX_SECONDS
        self.panel.render()
        self.assertEqual(self.frames, 2)


if __name__ == "__main__":
    unittest.main()
