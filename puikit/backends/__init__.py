"""Backend implementations and the backend factory."""

from __future__ import annotations

import sys

from ..backend import Backend


def create_backend(name: str, **kwargs) -> Backend:
    """Create a backend by name: "curses" (alias "tui"), "macos",
    "windows" (alias "win32"), "memory", or "gui" — the native GUI backend
    for the running platform (MacOSBackend on darwin, WindowsBackend on
    win32), so an app written against "gui" runs unmodified on either.

    Backends are imported lazily so that platform-specific modules are only
    loaded when actually requested.
    """
    name = name.lower()
    if name == "gui":
        name = "macos" if sys.platform == "darwin" else "windows"
    if name in ("curses", "tui"):
        from .curses_backend import CursesBackend

        return CursesBackend(**kwargs)
    if name == "macos":
        from .macos_backend import MacOSBackend

        return MacOSBackend(**kwargs)
    if name in ("windows", "win32"):
        from .windows_backend import WindowsBackend

        return WindowsBackend(**kwargs)
    if name == "memory":
        from .memory_backend import MemoryBackend

        return MemoryBackend(**kwargs)
    raise ValueError(f"unknown backend: {name!r}")


__all__ = ["create_backend"]
