"""Frame statistics for the Windows backend (``PUIKIT_FPS=1``).

A line logged once a second (logger ``puikit.backends._frame_stats``, INFO):

    draw 41.5/s new 30.6 | ui 29.6/s 18.6ms | render 24.7ms | present 0.4ms max 0.6 |
    inflight 0.88 max 1 | gpu 11.8ms | pipe nogil

- ``draw``: frames presented per second; ``new``: those showing a display list
  the previous frame had not (an animated background repaints the same list).
- ``ui``: display lists built per second on the UI thread, and the median
  time from ``clear()`` to ``present()`` - the widgets' draw pass.
- ``render``: median time of one frame on the thread that draws it, from the
  start of ``_render`` to the last EndDraw (Present excluded). Wall time, so
  a wait inside it (the render lock, the driver) counts.
- ``present``: median and worst time in Present. It returns at once unless the
  queue of frames waiting for the GPU is full, so a GPU that limits the frame
  rate shows here first.
- ``inflight``: frames submitted and not yet finished on the GPU as a frame
  starts. With frames drawn back to back (the render thread) about 1 is
  normal; stuck near the queue's limit (3) means the GPU cannot keep up.
- ``gpu``: median GPU time of a frame, from D3D11 timestamp queries around its
  commands; an upper bound, since a gap in submission counts too. A driver
  that reports every interval as disjoint (the VMware SVGA adapter does)
  gives no number; the reason is shown in its place.
- Last: ``seq`` or ``pipe`` (see _render_thread.py), and whether the GIL is on.

A development aid: off unless the variable is set, and costing nothing then.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import os
import statistics
import sys
import threading
import time
from typing import Any

ENV = "PUIKIT_FPS"

logger = logging.getLogger(__name__)

# ID3D11Device / ID3D11DeviceContext vtable slots (d3d11.h order).
_IDX_DEV_CREATE_QUERY = 24
_IDX_DEV_GET_IMMEDIATE_CONTEXT = 40
_IDX_CTX_BEGIN = 27
_IDX_CTX_END = 28
_IDX_CTX_GET_DATA = 29
_QUERY_TIMESTAMP_DISJOINT = 3
_QUERY_TIMESTAMP = 2
_GETDATA_DONOTFLUSH = 1
_S_OK = 0


class _QueryDesc(ctypes.Structure):
    _fields_ = [("Query", ctypes.c_uint32), ("MiscFlags", ctypes.c_uint32)]


class _Disjoint(ctypes.Structure):
    _fields_ = [("Frequency", ctypes.c_uint64), ("Disjoint", ctypes.c_int32)]


def wanted(environ: Any = None) -> bool:
    value = (os.environ if environ is None else environ).get(ENV, "")
    return value.strip().lower() not in ("", "0", "off", "false", "no")


class GpuTimer:
    """Timestamp queries around each frame, read back a few frames later so
    the CPU never waits for them. ``begin()`` / ``end()`` bracket a frame;
    ``collect()`` returns the GPU milliseconds of the frames that finished."""

    RING = 8

    def __init__(self, native: Any, device: Any) -> None:
        self._native = native
        self.device_addr = device.addr
        out = ctypes.c_void_p()
        device.call(_IDX_DEV_GET_IMMEDIATE_CONTEXT, None, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(out))
        self._ctx = native.ComPtr(out.value or 0)
        self._free: list[tuple] = []
        self._pending: collections.deque = collections.deque()
        self._open: tuple | None = None
        self.inflight = 0
        #: Why frames gave no GPU time: skipped (ring full), notready, disjoint, badts.
        self.misses: collections.Counter = collections.Counter()
        for _ in range(self.RING):
            self._free.append(tuple(self._query(device, kind) for kind in
                                    (_QUERY_TIMESTAMP_DISJOINT, _QUERY_TIMESTAMP, _QUERY_TIMESTAMP)))

    def _query(self, device: Any, kind: int) -> Any:
        desc = _QueryDesc(kind, 0)
        out = ctypes.c_void_p()
        hr = device.call(_IDX_DEV_CREATE_QUERY, ctypes.c_int32,
                         [ctypes.POINTER(_QueryDesc), ctypes.POINTER(ctypes.c_void_p)],
                         ctypes.byref(desc), ctypes.byref(out))
        if hr != _S_OK or not out.value:
            raise OSError(f"CreateQuery failed: 0x{hr & 0xFFFFFFFF:08x}")
        return self._native.ComPtr(out.value)

    def begin(self) -> None:
        if self._open is not None or not self._free:
            self.misses["skipped"] += 1     # the ring is full of frames still on the GPU
            return
        disjoint, start, _end = self._open = self._free.pop()
        self._ctx.call(_IDX_CTX_BEGIN, None, [ctypes.c_void_p], disjoint.addr)
        self._ctx.call(_IDX_CTX_END, None, [ctypes.c_void_p], start.addr)

    def end(self) -> None:
        if self._open is None:
            return
        disjoint, _start, end = self._open
        self._ctx.call(_IDX_CTX_END, None, [ctypes.c_void_p], end.addr)
        self._ctx.call(_IDX_CTX_END, None, [ctypes.c_void_p], disjoint.addr)
        self._pending.append(self._open)
        self._open = None

    def _get(self, query: Any, data: Any) -> bool:
        # A frame whose result is still out after a few more frames is asked
        # again with a flush, in case nothing else submitted the queries.
        flags = _GETDATA_DONOTFLUSH if len(self._pending) < self.RING else 0
        hr = self._ctx.call(_IDX_CTX_GET_DATA, ctypes.c_int32,
                            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32],
                            query.addr, ctypes.addressof(data), ctypes.sizeof(data), flags)
        if hr not in (_S_OK, 1):        # 1 = S_FALSE, not ready yet
            self.misses[f"hr 0x{hr & 0xFFFFFFFF:08x}"] += 1
        return hr == _S_OK

    def collect(self) -> list[float]:
        done = []
        while self._pending:
            disjoint, start, end = self._pending[0]
            dj, t0, t1 = _Disjoint(), ctypes.c_uint64(), ctypes.c_uint64()
            if not self._get(disjoint, dj):
                self.misses["notready"] += 1
                break
            ok = self._get(start, t0) and self._get(end, t1)
            self._pending.popleft()
            self._free.append((disjoint, start, end))
            if not ok:
                self.misses["notready"] += 1
            elif dj.Disjoint or not dj.Frequency:
                self.misses["disjoint"] += 1
            elif t1.value < t0.value:
                self.misses["badts"] += 1
            else:
                done.append((t1.value - t0.value) * 1000.0 / dj.Frequency)
        #: Frames submitted and not yet finished on the GPU, as this frame starts.
        self.inflight = len(self._pending)
        return done

    def release(self) -> None:
        for queries in [*self._free, *self._pending] + ([self._open] if self._open else []):
            for query in queries:
                query.release()
        self._free, self._pending, self._open = [], collections.deque(), None
        self._ctx.release()


class FrameStats:
    """The numbers behind the overlay line. The UI thread reports its draw
    passes; the thread that draws frames reports frames and GPU times and asks
    for the line."""

    WINDOW = 1.0

    def __init__(self, pipeline: bool) -> None:
        self._lock = threading.Lock()
        self._mode = "pipe" if pipeline else "seq"
        self._gil = "gil" if getattr(sys, "_is_gil_enabled", lambda: True)() else "nogil"
        self._ui_start: float | None = None
        self._ui: list[float] = []
        self._frames = 0
        self._new = 0
        self._render: list[float] = []
        self._gpu: list[float] = []
        self._present: list[float] = []
        self._inflight: list[int] = []
        self._last_front: int | None = None
        self._since = time.perf_counter()
        self.text = f"measuring... | {self._mode} {self._gil}"

    def ui_begin(self) -> None:
        self._ui_start = time.perf_counter()

    def ui_end(self) -> None:
        start, self._ui_start = self._ui_start, None
        if start is not None:
            with self._lock:
                self._ui.append((time.perf_counter() - start) * 1000.0)

    def frame(self, front: Any, render_ms: float, gpu_ms: list[float], gpu_note: str = "",
              present_ms: float | None = None, inflight: int | None = None) -> None:
        with self._lock:
            self._frames += 1
            if id(front) != self._last_front:
                self._new += 1
                self._last_front = id(front)
            self._render.append(render_ms)
            self._gpu.extend(gpu_ms)
            if present_ms is not None:
                self._present.append(present_ms)
            if inflight is not None:
                self._inflight.append(inflight)
            now = time.perf_counter()
            span = now - self._since
            if span < self.WINDOW:
                return
            ui, render, gpu = self._ui, self._render, self._gpu
            present, inflight = self._present, self._inflight

            def med(values: list[float]) -> str:
                return f"{statistics.median(values):.1f}ms" if values else "-"

            self.text = (f"draw {self._frames / span:.1f}/s new {self._new / span:.1f} | "
                         f"ui {len(ui) / span:.1f}/s {med(ui)} | render {med(render)} | "
                         f"present {med(present)} max {max(present, default=0):.1f} | "
                         f"inflight {sum(inflight) / len(inflight) if inflight else 0:.2f} max {max(inflight, default=0)} | "
                         f"gpu {med(gpu) if gpu else ('- ' + gpu_note).strip()} | {self._mode} {self._gil}")
            logger.info(self.text)
            self._frames = self._new = 0
            self._ui, self._render, self._gpu = [], [], []
            self._present, self._inflight = [], []
            self._since = now
