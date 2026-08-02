"""Fetch-and-cache for ``http(s)://`` image sources (internal).

Backends open image *files*; nothing below the widget layer knows URLs exist.
A widget that accepts an image source string (``MarkdownView``) funnels a
remote URL through :func:`get`: the first call starts a background download
into a shared cache directory and returns ``None`` — the caller lays out a
placeholder — and once the file is on disk (this call or any later one)
``get`` returns its local path, which then travels the ordinary
``draw_image`` pipeline untouched.

The download lands atomically (a ``.part`` file renamed into place), so a
path returned by :func:`get` always names a *complete* file. That matters
because the pipeline caches keyed by path string — the backends'
decoded-image dicts, the web backend's sent-asset set, the ``image_size``
LRU — would pin a half-written read forever; none of them may ever see the
path before the bytes are all there. A failed download is remembered for the
life of the process (the Windows backend treats an undecodable file the same
way) rather than re-attempted on every layout pass. The cache persists
across processes in the temp directory, so a previously seen URL renders
immediately.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import urllib.request
from typing import Callable
from urllib.parse import urlparse

# Generous but bounded: a README screenshot is hundreds of KB; nothing a
# markdown document embeds should be tens of MB. The timeout is per socket
# operation (urllib), not the whole transfer.
_TIMEOUT_S = 15.0
_MAX_BYTES = 32 * 1024 * 1024
# Some CDNs refuse urllib's default UA outright.
_USER_AGENT = "PuiKit-image-fetch"

_lock = threading.Lock()
_loading: set[str] = set()
_failed: set[str] = set()
_callbacks: dict[str, list[Callable[[str], None]]] = {}


def is_remote(src: str) -> bool:
    """Whether an image source string is a URL this module fetches."""
    return src.startswith(("http://", "https://"))


def cache_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "puikit-remote-images")


def cache_path(url: str) -> str:
    """The local file a URL downloads to: a digest name (stable across
    processes, safe for any URL) keeping the URL's image extension when it has
    a plausible one, purely as a courtesy to anyone listing the directory."""
    ext = os.path.splitext(urlparse(url).path)[1]
    if not (1 < len(ext) <= 8 and ext[1:].isalnum()):
        ext = ""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir(), digest + ext.lower())


def get(url: str, on_done: Callable[[str], None] | None = None) -> str | None:
    """The local path for ``url`` if its download has completed, else ``None``.

    A ``None`` with no prior failure means a fetch is now in flight (started
    here or by an earlier call); ``on_done`` — if given — fires once, **from
    the worker thread**, when that fetch settles either way, and the caller
    re-queries to learn which. Registering the same callback twice is a no-op,
    so a caller may pass it on every layout pass.
    """
    path = cache_path(url)
    if os.path.isfile(path):
        return path
    with _lock:
        if url in _failed:
            return None
        already = url in _loading
        if not already:
            _loading.add(url)
        if on_done is not None:
            cbs = _callbacks.setdefault(url, [])
            if on_done not in cbs:
                cbs.append(on_done)
    if not already:
        threading.Thread(
            target=_fetch, args=(url, path), daemon=True, name="puikit-image-fetch"
        ).start()
    return None


def _fetch(url: str, path: str) -> None:
    part = f"{path}.part-{os.getpid()}-{threading.get_ident()}"
    ok = False
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as resp, \
                open(part, "wb") as out:
            remaining = _MAX_BYTES
            while True:
                chunk = resp.read(min(65536, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise ValueError(f"image exceeds the {_MAX_BYTES}-byte cap")
                remaining -= len(chunk)
                out.write(chunk)
        os.replace(part, path)
        ok = True
    except Exception:
        # Silent by design, like every missing-image path in the backends; the
        # caller sees the failure as a permanent None from get().
        try:
            os.remove(part)
        except OSError:
            pass
    finally:
        with _lock:
            _loading.discard(url)
            if not ok:
                _failed.add(url)
            callbacks = _callbacks.pop(url, [])
        for cb in callbacks:
            try:
                cb(url)
            except Exception:
                pass
