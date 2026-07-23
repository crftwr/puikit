"""Backend implementations and the backend factory."""

from __future__ import annotations

import sys

from ..backend import Backend

# PyObjC ships each macOS framework as its own top-level module, and the macOS
# backend imports several at load time. PyObjC is an optional dependency (the
# ``macos`` extra), so a bare ``pip install puikit`` omits it; without this,
# requesting the backend fails with a bare ``No module named 'AppKit'`` that
# gives no hint about the fix.
_PYOBJC_MODULES = frozenset(
    {"AppKit", "Foundation", "objc", "PyObjCTools", "Quartz",
     "Cocoa", "CoreText", "CoreFoundation", "CoreGraphics"}
)


def _optional_dep_hint(err: ImportError, *, extra: str, dep: str) -> str | None:
    """If ``err`` is a missing optional backend dependency, return an install
    hint naming the extra; otherwise ``None`` (a genuine import error to
    re-raise so real bugs are not masked)."""
    if getattr(err, "name", None) in _PYOBJC_MODULES:
        return (
            f'the {dep} package is required for the "{extra}" backend but is '
            f'not installed. Install it with:  pip install "puikit[{extra}]"'
        )
    return None


def create_backend(name: str, **kwargs) -> Backend:
    """Create a backend by name: "curses" (alias "tui"), "macos",
    "windows" (alias "win32"), "web" (aliases "webbrowser"/"browser"),
    "memory", or "gui" — the native GUI backend for the running platform
    (MacOSBackend on darwin, WindowsBackend on win32), so an app written
    against "gui" runs unmodified on either.

    Backends are imported lazily so that platform-specific modules are only
    loaded when actually requested.
    """
    name = name.lower()
    if name == "gui":
        name = "macos" if sys.platform == "darwin" else "windows"
    if name in ("curses", "tui"):
        from .curses_backend import CursesBackend

        return CursesBackend(**kwargs)
    if name in ("web", "webbrowser", "browser"):
        from .web_backend import WebBackend

        return WebBackend(**kwargs)
    if name == "macos":
        try:
            from .macos_backend import MacOSBackend
        except ImportError as e:
            hint = _optional_dep_hint(e, extra="macos", dep="PyObjC")
            if hint is not None:
                raise ImportError(hint) from e
            raise

        return MacOSBackend(**kwargs)
    if name in ("windows", "win32"):
        from .windows_backend import WindowsBackend

        return WindowsBackend(**kwargs)
    if name == "memory":
        from .memory_backend import MemoryBackend

        return MemoryBackend(**kwargs)
    raise ValueError(f"unknown backend: {name!r}")


__all__ = ["create_backend"]
