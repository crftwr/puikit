"""Decoding image files off the UI thread: what the GUI backends share (xefm#732).

A backend's own decoder (ImageIO on macOS, WIC on Windows) does the work; this
module holds what is the same on both:

- :class:`ImageLoader`, one worker thread serving queued requests newest
  first, delivering each result on the UI thread - through the backend's
  ``post``, or straight away when the frame waits for it (:meth:`ImageLoader.wait`);
- :func:`needed_side` / :func:`wants_redecode`, how many pixels a draw needs
  and when the held bitmap should be decoded again at another size;
- :class:`Decoded`, what a backend caches per source.

The design and the measurements are in docs/images.md section 11.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Callable

#: How long one frame may wait, in total, for the worker to finish pictures it
#: asked for. A small picture decodes in about a millisecond and is then drawn in
#: the frame that asked for it, instead of a blank frame and a redraw; a large
#: one is not waited for. Bounded per frame, so a page of thirty icons costs the
#: UI thread this much at most, and a file on a slow disk nothing more.
FRAME_WAIT = 0.004

#: A held bitmap is re-decoded smaller only when it is this many times larger
#: than needed, so a window resized back and forth does not decode on every
#: step. Needing more is re-decoded at once: a blurry picture is the visible
#: failure, a too-sharp one is not.
SHRINK_FACTOR = 1.5

#: Requests queued at once. The newest are served first; the oldest beyond
#: this are dropped, which is what a viewer stepping quickly through a
#: directory needs - only the picture now on screen matters.
QUEUE_LIMIT = 8


@dataclass
class Decoded:
    """A drawable image and what is known about the pixels behind it."""

    #: What the backend draws: on macOS an ``NSImage`` sized in the source's own
    #: points, so the fit and ``src`` math sees the size a lazily loaded file
    #: reported; on Windows ``(ID2D1Bitmap, width, height)`` of the bitmap.
    image: Any
    #: Long side, in pixels, of the bitmap behind ``image``. 0 for the lazy
    #: fallback, which decodes at whatever size it is drawn.
    pixels: int = 0
    #: Long side of the source at full resolution. 0 when unknown (the
    #: fallback), and then no other size is ever asked for.
    full: int = 0
    #: What the cache weighs the entry at.
    nbytes: int = 0
    #: The source's own size, which the fit and ``src`` math work in, when
    #: ``image`` does not carry it (Windows: the upright full-size pixels).
    size: tuple[int, int] | None = None


def needed_side(dest_w: float, dest_h: float, src_w: float, src_h: float,
                image_w: float, image_h: float, scale: float) -> int:
    """Pixels the image's long side needs so that the ``src_w`` x ``src_h``
    part of it (in the image's own units) fills ``dest_w`` x ``dest_h`` points
    at ``scale`` device pixels per point."""
    if src_w <= 0 or src_h <= 0 or image_w <= 0 or image_h <= 0:
        return 0
    per_unit = max(dest_w / src_w, dest_h / src_h) * scale
    return max(1, math.ceil(max(image_w, image_h) * per_unit))


def wants_redecode(entry: Decoded, need: int) -> bool:
    """Whether ``entry`` should be decoded again for a need of ``need`` pixels
    on its long side."""
    if entry.full <= 0 or entry.pixels <= 0 or need <= 0:
        return False
    need = min(need, entry.full)
    if need > entry.pixels:
        return True
    return entry.pixels > need * SHRINK_FACTOR


#: Handed to ``on_done`` for a request that was dropped unserved.
DROPPED = object()


class ImageLoader:
    """One worker thread decoding queued requests, newest first.

    ``post(fn, *args)`` must run ``fn(*args)`` on the UI thread; every
    ``on_done(key, result)`` arrives through it, or through :meth:`wait` when
    the UI thread chose to wait for it, ``result`` being a
    :class:`Decoded` (or whatever ``decoder`` returns), ``None`` for a file
    that cannot be read, or :data:`DROPPED`. ``decoder(path, side)`` runs on
    the worker thread."""

    def __init__(self, on_done: Callable[[Any, Any], None],
                 post: Callable[..., None],
                 decoder: Callable[[Any, Any], Any],
                 limit: int = QUEUE_LIMIT):
        self._on_done = on_done
        self._post = post
        self._decode = decoder
        self._limit = max(1, limit)
        self._jobs: list[tuple[Any, Any, Any]] = []
        self._ready: dict[Any, Any] = {}   # key -> result, until delivered
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, key: Any, path: Any, side: Any) -> None:
        dropped = []
        with self._cond:
            if self._closed:
                return
            self._jobs.append((key, path, side))
            while len(self._jobs) > self._limit:
                dropped.append(self._jobs.pop(0)[0])
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="puikit-image-decode", daemon=True)
                self._thread.start()
            self._cond.notify()
        for key in dropped:
            self._post(self._on_done, key, DROPPED)

    def wait(self, key: Any, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for ``key``'s result and, when it
        comes, deliver it now (``on_done`` on this, the UI thread) instead of
        through ``post``. Whether it was delivered."""
        with self._cond:
            if key not in self._ready and timeout > 0:
                self._cond.wait_for(lambda: key in self._ready or self._closed,
                                    timeout)
        return self._deliver(key)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._jobs.clear()
            self._ready.clear()
            self._cond.notify_all()

    def _deliver(self, key: Any) -> bool:
        with self._cond:
            if key not in self._ready:
                return False   # already delivered by wait(), or not done yet
            result = self._ready.pop(key)
        self._on_done(key, result)
        return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._jobs and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                key, path, side = self._jobs.pop()
            try:
                result = self._decode(path, side)
            except Exception:
                result = None
            with self._cond:
                if self._closed:
                    return
                self._ready[key] = result
                self._cond.notify_all()
            self._post(self._deliver, key)
