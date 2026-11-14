"""A render thread for a GUI backend: the frame pipeline of xefm Discussion #901.

A frame runs three stages. The widgets' ``draw()`` build a display list (1),
the backend replays it into the native drawing API (2), and the GPU flushes
and presents it (3). Run one after another on the UI thread, the frame takes
the sum of the three. With this thread, the UI thread builds frame N+1's
display list while the render thread replays and presents frame N, so a burst
of frames (a key held down) runs at the pace of the slowest stage instead.

The hand-over is the display list the backend already double-buffers: a
presented list is never appended to again (``present`` swaps in a fresh one),
so the render thread reads it without a copy. Requests coalesce: frames asked
for while one is being drawn become one more frame, of the newest list. A
single frame asked for while the thread is idle starts at once, so a lone key
press costs no latency; only a burst pays the one frame of overlap.

``lock`` makes the render thread's frame exclusive. The UI thread takes it
around anything that changes what the frame reads from and does not
replace atomically: the swap chain on a resize, the text formats on a DPI or
font change, the effect chain, the image cache. The lock is reentrant, so a
frame that calls back into one of those paths (an image delivered during the
frame's own wait) does not deadlock against itself.

Whether a backend runs one is :func:`pipeline_wanted`: on by default only on a
free-threaded build, where the two threads really run at once. On a GIL build
they would take turns at the interpreter, and the overlap is only what each
spends outside it (the native calls), so it stays off there unless asked for.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable

_logger = logging.getLogger(__name__)

#: ``PUIKIT_RENDER_THREAD``: ``1`` runs the render thread, ``0`` does not, and
#: unset (or ``auto``) runs it on a free-threaded build only.
ENV = "PUIKIT_RENDER_THREAD"


def free_threaded() -> bool:
    """Whether this interpreter is running without the GIL right now."""
    is_enabled = getattr(sys, "_is_gil_enabled", None)
    return is_enabled is not None and not is_enabled()


def pipeline_wanted(environ: dict | None = None) -> bool:
    """Whether a backend should render on a thread of its own (see the module
    docstring)."""
    raw = (os.environ if environ is None else environ).get(ENV, "").strip().lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return free_threaded()


class RenderThread:
    """Calls ``render`` on a thread of its own, once per :meth:`request`,
    coalescing the requests that arrive while a frame is being drawn.

    ``render`` runs holding :attr:`lock`. ``on_start`` runs once on the new
    thread before the first frame (per-thread setup such as COM
    initialization)."""

    def __init__(self, render: Callable[[], None], *, name: str = "puikit-render",
                 on_start: Callable[[], None] | None = None) -> None:
        self._render = render
        self._on_start = on_start
        self.lock = threading.RLock()
        self._cond = threading.Condition(threading.Lock())
        self._pending = False
        self._busy = False
        self._stopping = False
        #: Frames drawn, and requests folded into a frame already owed.
        self.frames = 0
        self.coalesced = 0
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def ident(self) -> int | None:
        return self._thread.ident

    def on_thread(self) -> bool:
        return threading.get_ident() == self._thread.ident

    def request(self) -> None:
        """Ask for a frame. Never blocks: a frame already owed absorbs this one,
        and the thread draws whatever the newest display list is when it gets
        there."""
        with self._cond:
            if self._stopping:
                return
            if self._pending:
                self.coalesced += 1
            self._pending = True
            self._cond.notify()

    def idle(self) -> bool:
        with self._cond:
            return not self._pending and not self._busy

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until no frame is owed or being drawn (tests, and a caller that
        must see the frame on screen). Whether it got there in time."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._pending or self._busy:
                left = deadline - time.monotonic()
                if left <= 0 or self._stopping:
                    return not (self._pending or self._busy)
                self._cond.wait(left)
            return True

    def stop(self, timeout: float = 5.0) -> None:
        """Finish the frame being drawn, draw no more, and end the thread."""
        with self._cond:
            self._stopping = True
            self._pending = False
            self._cond.notify_all()
        if self._thread.is_alive() and not self.on_thread():
            self._thread.join(timeout)

    def _run(self) -> None:
        if self._on_start is not None:
            try:
                self._on_start()
            except Exception:
                _logger.exception("render thread setup failed")
        while True:
            with self._cond:
                while not self._pending and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    self._cond.notify_all()
                    return
                self._pending = False
                self._busy = True
            try:
                with self.lock:
                    self._render()
            except Exception:
                # A frame that raises is logged and dropped; the next request
                # draws again rather than the thread dying with the window up.
                _logger.exception("render thread frame failed")
            finally:
                with self._cond:
                    self._busy = False
                    self.frames += 1
                    self._cond.notify_all()
