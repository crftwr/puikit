"""The "tui" alias names the kind; the VT backend is now the implementation."""

import sys

import pytest

from puikit.backends import create_backend
from puikit.backends.curses_backend import CursesBackend
from puikit.backends.vt_backend import VTBackend


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_tui_is_the_vt_backend_everywhere(monkeypatch, platform):
    # On Windows because PDCurses breaks full-width column pitch (xefm#283) and
    # swallows inline images (xefm#306); on macOS and Linux because the POSIX
    # console made the same batched-write, wide-glyph, image-capable engine
    # available where ncurses was merely not broken.
    monkeypatch.setattr("sys.platform", platform)
    assert isinstance(create_backend("tui"), VTBackend)


def test_curses_stays_reachable_as_the_escape_hatch(monkeypatch):
    # For the terminals the VT console's xterm-dialect assumption mishandles
    # (TERM=linux consoles, serial lines).
    monkeypatch.setattr("sys.platform", "win32")
    assert isinstance(create_backend("curses"), CursesBackend)
    monkeypatch.setattr("sys.platform", "linux")
    assert isinstance(create_backend("curses"), CursesBackend)


def test_gui_alias_is_unchanged():
    # Only on real Windows: importing windows_backend loads Win32 DLLs, so no
    # amount of sys.platform patching lets this run elsewhere — with numpy
    # absent it died on the import, and with numpy present on ctypes.WinDLL.
    if sys.platform != "win32":
        pytest.skip("WindowsBackend needs the real Win32 DLLs")
    from puikit.backends.windows_backend import WindowsBackend

    assert isinstance(create_backend("gui"), WindowsBackend)
