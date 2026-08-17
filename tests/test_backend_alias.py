"""The "tui" alias names the kind; the VT backend is now the implementation."""

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


def test_gui_alias_is_unchanged(monkeypatch):
    from puikit.backends.windows_backend import WindowsBackend

    monkeypatch.setattr("sys.platform", "win32")
    assert isinstance(create_backend("gui"), WindowsBackend)
