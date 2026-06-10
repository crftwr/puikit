"""Backend implementations and the backend factory."""

from __future__ import annotations

from ..backend import Backend


def create_backend(name: str, **kwargs) -> Backend:
    """Create a backend by name: "curses" (alias "tui"), "macos"
    (alias "gui"), or "memory".

    Backends are imported lazily so that platform-specific modules are only
    loaded when actually requested.
    """
    name = name.lower()
    if name in ("curses", "tui"):
        from .curses_backend import CursesBackend

        return CursesBackend(**kwargs)
    if name in ("macos", "gui"):
        from .macos_backend import MacOSBackend

        return MacOSBackend(**kwargs)
    if name == "memory":
        from .memory_backend import MemoryBackend

        return MemoryBackend(**kwargs)
    raise ValueError(f"unknown backend: {name!r}")


__all__ = ["create_backend"]
