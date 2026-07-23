"""Backend implementations and the backend factory."""

from __future__ import annotations

import sys

from ..backend import Backend

# PyObjC ships each macOS framework as its own top-level module, and the macOS
# backend imports several at load time. It is a darwin-marked base dependency,
# so a normal macOS ``pip install puikit`` already includes it — but a
# ``--no-deps`` install (or requesting this backend off macOS) can leave it
# absent, where the bare ``No module named 'AppKit'`` gives no hint about the
# fix. Map those misses to a clear message instead.
_PYOBJC_MODULES = frozenset(
    {"AppKit", "Foundation", "objc", "PyObjCTools", "Quartz",
     "Cocoa", "CoreText", "CoreFoundation", "CoreGraphics"}
)


def _pyobjc_hint(err: ImportError) -> str | None:
    """If ``err`` is a missing PyObjC framework (which the macOS backend needs),
    return a clear install hint; otherwise ``None`` — a genuine, unrelated
    import error the caller re-raises so real bugs are not masked."""
    if getattr(err, "name", None) in _PYOBJC_MODULES:
        return (
            "the macOS backend requires PyObjC, which installs automatically "
            "with `pip install puikit` on macOS. If it is missing (e.g. a "
            "--no-deps install), run:  "
            "pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz"
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
            hint = _pyobjc_hint(e)
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
