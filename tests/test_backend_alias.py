"""The "tui" alias resolves per platform, like "gui" does."""

import pytest

from puikit.backends import create_backend
from puikit.backends.curses_backend import CursesBackend
from puikit.backends.vt_backend import VTBackend


def test_tui_is_the_vt_backend_on_windows(monkeypatch):
    # curses on Windows is PDCurses, whose cell model breaks the column pitch of
    # full-width glyphs (xefm#283) and whose private screen buffer swallows
    # inline images (xefm#306). Neither is fixable from outside it.
    monkeypatch.setattr("sys.platform", "win32")
    assert isinstance(create_backend("tui"), VTBackend)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_tui_is_curses_everywhere_else(monkeypatch, platform):
    # ncurses is not broken there, and terminfo still earns its keep across the
    # range of terminals a Unix TUI meets.
    monkeypatch.setattr("sys.platform", platform)
    assert isinstance(create_backend("tui"), CursesBackend)


def test_both_stay_reachable_by_their_own_name(monkeypatch):
    # "curses" is the escape hatch on Windows; "vt" the opt-in elsewhere.
    monkeypatch.setattr("sys.platform", "win32")
    assert isinstance(create_backend("curses"), CursesBackend)
    monkeypatch.setattr("sys.platform", "linux")
    assert isinstance(create_backend("vt"), VTBackend)


def test_gui_alias_is_unchanged(monkeypatch):
    from puikit.backends.windows_backend import WindowsBackend

    monkeypatch.setattr("sys.platform", "win32")
    assert isinstance(create_backend("gui"), WindowsBackend)
