"""Closing a macOS window must break what Python's gc cannot see (Keyhac #325),
and take the window out of the backend's per-window records (Keyhac #377)."""

import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit")


def _handle():
    from puikit.backends.macos_backend import _NO_CURSOR_WINDOW, MacWindowHandle
    nswindow = SimpleNamespace(setDelegate_=lambda d: None, orderOut_=lambda s: None)
    view = SimpleNamespace(pk_window=None)
    delegate = SimpleNamespace(handle=None)
    backend = SimpleNamespace(_secondary_windows=[], _clear_colors={},
                              _pointer_cursors={}, _cursor_window=_NO_CURSOR_WINDOW)
    handle = MacWindowHandle(backend, nswindow, view, delegate, None)
    view.pk_window = handle
    delegate.handle = handle
    backend._secondary_windows.append(handle)
    return handle, view, delegate


def _recorded(handle):
    """What rendering and hovering the window leave behind: a clear color
    every frame, a pointer shape and the pointer's window while it is over it."""
    backend = handle._backend
    backend._clear_colors[handle] = (30, 30, 30)
    backend._clear_colors[None] = (0, 0, 0)          # the main window's
    backend._pointer_cursors[handle] = ("text", None)
    backend._cursor_window = handle
    return backend


def test_close_breaks_both_references_back_to_the_handle():
    handle, view, delegate = _handle()
    handle.close()
    assert view.pk_window is None and delegate.handle is None


def test_a_close_by_the_user_breaks_them_too():
    handle, view, delegate = _handle()
    closed = []
    handle.on_close = lambda: closed.append(delegate.handle)
    handle._on_user_close()
    assert view.pk_window is None and delegate.handle is None
    assert closed == [None]


def test_close_takes_the_window_out_of_the_backends_records():
    # Each is keyed by the handle, and the handle holds the app's window object
    # through on_event / on_close: a record left behind kept a closed chooser,
    # its rows and its NSWindow alive for good (Keyhac #377).
    from puikit.backends.macos_backend import _NO_CURSOR_WINDOW
    handle, _view, _delegate = _handle()
    backend = _recorded(handle)
    handle.close()
    assert handle not in backend._clear_colors
    assert handle not in backend._pointer_cursors
    assert backend._cursor_window is _NO_CURSOR_WINDOW
    assert backend._clear_colors == {None: (0, 0, 0)}


def test_a_close_by_the_user_takes_it_out_too():
    handle, _view, _delegate = _handle()
    backend = _recorded(handle)
    handle._on_user_close()
    assert handle not in backend._clear_colors
    assert handle not in backend._pointer_cursors


def test_closing_another_window_leaves_the_pointers_window_alone():
    first, _v1, _d1 = _handle()
    second, _v2, _d2 = _handle()
    second._backend = first._backend
    backend = _recorded(first)
    second.close()
    assert backend._cursor_window is first
    assert first in backend._clear_colors
