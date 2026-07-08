"""Pure-logic tests for the Windows drag & drop plumbing (_win32_dragdrop.py)
that don't need a live OLE drag session or a registered drop target — those
paths (IDropSource/IDropTarget actually invoked by OLE, a real cross-window
drop) were verified live against real windows during development; see
test_windows_backend.py for the window-based tests that don't need input
injection."""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only backend")
pytest.importorskip("ctypes.wintypes", reason="Windows-only backend")

import ctypes  # noqa: E402

from puikit.backends import _win32_dragdrop as dd  # noqa: E402


def test_drop_effects_mask_combines_requested_operations():
    assert dd.drop_effects_mask(("copy",)) == dd.DROPEFFECT_COPY
    assert dd.drop_effects_mask(("move",)) == dd.DROPEFFECT_MOVE
    assert dd.drop_effects_mask(("copy", "move")) == dd.DROPEFFECT_COPY | dd.DROPEFFECT_MOVE
    # An unrecognized/empty operation set falls back to copy rather than 0
    # (DoDragDrop with dwOKEffects=0 would offer no operation at all).
    assert dd.drop_effects_mask(()) == dd.DROPEFFECT_COPY


def test_drop_effect_name_round_trips_drop_effects_mask():
    assert dd.drop_effect_name(dd.DROPEFFECT_COPY) == "copy"
    assert dd.drop_effect_name(dd.DROPEFFECT_MOVE) == "move"
    assert dd.drop_effect_name(dd.DROPEFFECT_LINK) == "link"
    assert dd.drop_effect_name(dd.DROPEFFECT_NONE) == "none"
    assert dd.drop_effect_name(0x9999) == "none"  # unrecognized -> none, never raises


def test_guid_eq_matches_identical_and_rejects_different():
    a = dd.GUID.from_str("00000121-0000-0000-C000-000000000046")
    b = dd.GUID.from_str("00000121-0000-0000-C000-000000000046")
    c = dd.GUID.from_str("00000122-0000-0000-C000-000000000046")
    assert dd._guid_eq(a, b)
    assert not dd._guid_eq(a, c)


def test_matches_hdrop_checks_format_aspect_and_tymed():
    exact = dd.FORMATETC(dd.CF_HDROP, None, dd.DVASPECT_CONTENT, -1, dd.TYMED_HGLOBAL)
    assert dd._matches_hdrop(exact)
    wrong_format = dd.FORMATETC(13, None, dd.DVASPECT_CONTENT, -1, dd.TYMED_HGLOBAL)  # CF_UNICODETEXT
    assert not dd._matches_hdrop(wrong_format)
    wrong_tymed = dd.FORMATETC(dd.CF_HDROP, None, dd.DVASPECT_CONTENT, -1, 4)  # TYMED_ISTREAM, not TYMED_HGLOBAL
    assert not dd._matches_hdrop(wrong_tymed)


def test_file_data_object_query_get_data_and_get_data_round_trip(tmp_path):
    path = tmp_path / "dragged.txt"
    path.write_text("hi")
    obj = dd.create_file_data_object([str(path)])
    assert obj is not None

    fmt = dd.FORMATETC(dd.CF_HDROP, None, dd.DVASPECT_CONTENT, -1, dd.TYMED_HGLOBAL)
    hr = obj._query_get_data(obj.addr, ctypes.pointer(fmt))
    assert hr == 0  # S_OK

    medium = dd.STGMEDIUM()
    hr = obj._get_data(obj.addr, ctypes.pointer(fmt), ctypes.pointer(medium))
    assert hr == 0
    assert medium.hGlobal

    # The HGLOBAL is a real HDROP: DragQueryFileW must read our path back.
    count = dd.shell32.DragQueryFileW(medium.hGlobal, dd._DRAGQUERY_FILE_COUNT, None, 0)
    assert count == 1
    length = dd.shell32.DragQueryFileW(medium.hGlobal, 0, None, 0)
    buf = ctypes.create_unicode_buffer(length + 1)
    dd.shell32.DragQueryFileW(medium.hGlobal, 0, buf, length + 1)
    assert buf.value == str(path)
    dd.ole32.ReleaseStgMedium(ctypes.byref(medium))


def test_file_data_object_rejects_non_hdrop_format():
    obj = dd.create_file_data_object(["C:\\does\\not\\matter.txt"])
    fmt = dd.FORMATETC(13, None, dd.DVASPECT_CONTENT, -1, dd.TYMED_HGLOBAL)  # CF_UNICODETEXT, not CF_HDROP
    assert obj._query_get_data(obj.addr, ctypes.pointer(fmt)) == 1  # S_FALSE
    medium = dd.STGMEDIUM()
    hr = obj._get_data(obj.addr, ctypes.pointer(fmt), ctypes.pointer(medium))
    assert hr == dd.DV_E_FORMATETC


def test_create_file_data_object_returns_none_for_empty_paths():
    assert dd.create_file_data_object([]) is None
