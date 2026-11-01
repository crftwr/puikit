"""Decoding a shader background's textures off the UI thread.

A :class:`~puikit.background.Shader` that names textures hands the backend
several photographs to decode — a 4096-pixel world map takes a couple of hundred
milliseconds in ImageIO — and the backend learns about them inside
``set_background``, which runs on the UI thread when a theme is applied. Decoding
there would freeze the window for exactly the moment the user is watching it
change, so the decode runs on a worker and the renderer polls for it.

What crosses threads is only CPU data: the worker produces
:class:`~puikit.image.RasterImage` buffers, and the renderer uploads them on the
thread that owns the GPU context. Neither Metal's command queue nor a D3D11
immediate context is touched from the worker.

The decoder is the operating system's own (:mod:`puikit._platform_image`), the
one every backend already trusts for images: ImageIO on macOS, WIC on Windows.
Where there is none (Linux) a load fails, and a failed load means the backend
draws the bare backdrop — the same outcome as a shader that fails to compile.
"""

from __future__ import annotations

import os
import threading
from typing import Any


class TextureLoad:
    """The decode of one shader's textures, started on construction.

    ``rasters`` is the decoded list, in slot order, once :attr:`done` and not
    :attr:`failed`; ``error`` names what failed. Each instance is used once — a
    new texture set starts a new load, and an abandoned one simply finishes on
    its own thread and is dropped.
    """

    def __init__(self, textures: "tuple[Any, ...]") -> None:
        self.textures = tuple(textures)
        self.rasters: "list[Any] | None" = None
        self.error: str | None = None
        self._done = threading.Event()
        if not self.textures:
            self.rasters = []
            self._done.set()
            return
        threading.Thread(target=self._run, name="puikit-bg-textures",
                         daemon=True).start()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def failed(self) -> bool:
        return self._done.is_set() and self.rasters is None

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the decode finishes; True when it has."""
        return self._done.wait(timeout)

    def _run(self) -> None:
        from .. import _platform_image
        rasters = []
        try:
            for tex in self.textures:
                path = os.path.expanduser(tex.path)
                raster = _platform_image.decode(path)
                if raster is None:
                    self.error = f"texture could not be decoded: {path}"
                    return
                rasters.append(raster)
            self.rasters = rasters
        except Exception as exc:  # a decoder failure must not kill the thread silently
            self.error = f"texture load failed: {exc}"
        finally:
            self._done.set()
