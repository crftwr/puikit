"""Closing a macOS window must break what Python's gc cannot see (Keyhac #325)."""

import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppKit")


def _handle():
    from puikit.backends.macos_backend import MacWindowHandle
    nswindow = SimpleNamespace(setDelegate_=lambda d: None, orderOut_=lambda s: None)
    view = SimpleNamespace(pk_window=None)
    delegate = SimpleNamespace(handle=None)
    backend = SimpleNamespace(_secondary_windows=[])
    handle = MacWindowHandle(backend, nswindow, view, delegate, None)
    view.pk_window = handle
    delegate.handle = handle
    backend._secondary_windows.append(handle)
    return handle, view, delegate


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
