"""Frame-phase profiling, enabled by ``PUIKIT_FRAME_PROFILE``.

Answers "where did that frame go?" on the *real* window — which an offscreen
benchmark cannot, because the expensive parts (the per-glyph rasterization, the
shader pass, the post-effect graph, Present) only exist against a live device.

Set ``PUIKIT_FRAME_PROFILE=1`` to write to ``puikit_frames.log`` in the temp
directory, or set it to a path to choose the file. One line per frame, plus a
marker line whenever a layer is pushed or popped, so the frames belonging to a
dialog opening can be found in the trace.

Off by default and free when off: every caller guards its ``perf_counter`` calls
on :data:`ENABLED` rather than merely discarding the result, so this can stay in
place permanently. Follows the ``PUIKIT_BG_PROFILE`` precedent in the macOS
backend, one level up so the Panel and a backend can write to the same trace.
"""

from __future__ import annotations

import os
import tempfile
import time

_env = os.environ.get("PUIKIT_FRAME_PROFILE")

#: Whether profiling is on. Callers test this *before* timing anything.
ENABLED = bool(_env)

_path = (_env if _env and _env not in ("1", "true", "yes")
         else os.path.join(tempfile.gettempdir(), "puikit_frames.log"))
_file = None
_t0 = 0.0


def write(line: str) -> None:
    """Append ``line`` to the trace, prefixed with ms since the first write.

    Line-buffered and flushed per line: a trace is read after a crash or a kill
    as often as after a clean exit, and the write cost is part of what the
    profiled frame honestly costs anyway.
    """
    global _file, _t0
    if not ENABLED:
        return
    now = time.perf_counter()
    if _file is None:
        _t0 = now
        try:
            _file = open(_path, "w", encoding="utf-8")
        except OSError:
            return
        _file.write(f"# puikit frame profile -> {_path}\n")
    _file.write(f"{(now - _t0) * 1000:9.1f}  {line}\n")
    _file.flush()


def path() -> str:
    """Where the trace is being written (for a startup banner)."""
    return _path
