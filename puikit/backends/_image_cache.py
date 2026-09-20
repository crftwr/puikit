"""A byte-budgeted LRU for decoded images, shared by the two GUI backends.

Both of them used to keep ``{path: decoded}`` and never drop anything. That was
defensible while the only thing that could land in it was a file on disk: a
window shows a bounded number of distinct pictures, and re-decoding one per
frame — which is what the dict was added to stop — costs far more than holding
it. What changes the arithmetic is :class:`~puikit.image.RasterImage`. An
application that decodes its own pictures can hand over a 24-megapixel photo as
100 MB of RGBA, and a viewer walking a directory of them would hand over a new
one per keypress. Unbounded stops being a reasonable bet at that size.

So the entries are weighed, in the only currency that matters here — the bytes
the decoded picture occupies, which is ``width * height * 4`` whatever the file
it came from weighed. When the total is over budget the least recently used
entries go, and a backend holding a native handle (an ``ID2D1Bitmap``, say) is
told so it can release it.

Two things it is careful about:

**A failed decode is a result.** A missing or corrupt image must be attempted
once, not once per frame, so ``None`` is a value the cache stores and returns;
:data:`MISS` is what "nothing is stored here" looks like, and the two are not
the same answer.

**Nothing is ever evicted to make room for itself.** An image bigger than the
whole budget is still drawn, and still cached — alone. The alternative is a
cache that refuses precisely the images that most need one.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable

#: Returned by :meth:`ImageCache.get` when the key is not held at all — as
#: distinct from being held with the value ``None``, which is what a decode that
#: failed is remembered as.
MISS = object()

#: Default budget: enough for a handful of full-screen pictures, or one very
#: large one, and small enough that a directory of photos walked end to end does
#: not hold all of them. Backends may pass their own.
DEFAULT_BUDGET = 128 * 1024 * 1024


class ImageCache:
    """Decoded images by source identity (``puikit.image.source_key``), under a
    total byte budget, least-recently-used evicted first."""

    def __init__(self, budget: int = DEFAULT_BUDGET,
                 on_evict: Callable[[Any], None] | None = None):
        self._entries: "OrderedDict[Any, tuple[Any, int]]" = OrderedDict()
        self._budget = max(1, int(budget))
        self._bytes = 0
        # Called with the evicted value so a backend can release a native handle
        # the garbage collector knows nothing about. Never called for ``None``,
        # which is a remembered failure and owns nothing.
        self._on_evict = on_evict

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def nbytes(self) -> int:
        """Bytes currently held — what the budget is spent against."""
        return self._bytes

    def get(self, key: Any) -> Any:
        """The value stored under ``key``, or :data:`MISS`. A hit is marked most
        recently used, which is the whole basis of the eviction order."""
        entry = self._entries.get(key)
        if entry is None:
            return MISS
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: Any, value: Any, nbytes: int = 0) -> None:
        """Store ``value`` under ``key``, weighing it at ``nbytes``, then evict
        until the total is back inside the budget."""
        self._drop(key)
        weight = max(0, int(nbytes))
        self._entries[key] = (value, weight)
        self._bytes += weight
        while self._bytes > self._budget and len(self._entries) > 1:
            self._drop(next(iter(self._entries)))

    def clear(self) -> None:
        """Drop everything, releasing each held value. A backend calls this when
        the device its bitmaps belong to goes away."""
        for key in list(self._entries):
            self._drop(key)

    def _drop(self, key: Any) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        value, weight = entry
        self._bytes -= weight
        if value is not None and self._on_evict is not None:
            self._on_evict(value)
