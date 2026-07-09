"""Drag & drop support for the Windows backend — both directions.

Both directions need a real OLE drag session (``DoDragDrop`` on the source
side, ``RegisterDragDrop`` + a live ``IDropTarget`` on the receiving side) —
an earlier version of this module tried the classic ``DragAcceptFiles`` /
``WM_DROPFILES`` shortcut for drop-in to avoid hand-building a COM object at
all, but that shortcut turned out not to fire for an arbitrary OLE source in
practice (verified live: a cross-window drop from a plain ``DoDragDrop``
source never produced a ``WM_DROPFILES`` message, regardless of
``OleInitialize`` ordering) — real ``IDropTarget`` registration is what
Explorer and every other OLE-aware app actually rely on, so that's what this
module builds instead.

**Drag-out** (capability ``os_drag_drop``) needs a data object carrying the
dragged files, and a drop source answering "keep going?" as the mouse moves.
The data object is ``FileDataObject`` — a *hand-built* ``IDataObject``
exposing exactly one format (CF_HDROP over a real ``DROPFILES`` global memory
block; see its docstring for why this replaced an earlier attempt to get it
for free from the shell's ``SHCreateDataObject``).

**Drop-in** (capability ``drag_and_drop``) registers a hand-built
``IDropTarget`` (``RegisterDragDrop``) that only cares whether the incoming
``IDataObject`` offers CF_HDROP (``QueryGetData``) and, on an actual drop,
reads it (``GetData`` + the same ``DragQueryFileW`` shell32 export the old
``WM_DROPFILES`` path would have used — ``DragQueryFileW`` happily reads an
``HDROP`` from *any* source, not just a ``WM_DROPFILES`` message).

Three hand-built COM objects live here — ``IDropSource`` (2 real methods),
``IDropTarget`` (4 real methods), ``FileDataObject`` (``IDataObject``, 2 real
methods) — all small enough to author their vtables directly in ctypes: a
``ctypes.Structure`` of ``WINFUNCTYPE`` callback pointers standing in for the
vtable, wrapping an object instance whose only field is a pointer to it. This
whole pipeline was verified live end to end: a real window granted mouse
capture, ``DoDragDrop`` called synchronously from its ``WM_MOUSEMOVE``
handler (the exact shape ``WindowsBackend.begin_file_drag`` uses) completed
cleanly with our ``IDropSource`` vtable actually invoked by OLE; a second,
separate window registered as an ``IDropTarget`` correctly received
``DragEnter``/``DragOver``/``Drop`` and extracted the dragged path from a
``FileDataObject`` built by the first window's drag session.

One easy-to-miss requirement, needed for *both* directions: COM apartment
state is **per-thread**. ``OleInitialize`` must have run on the calling
thread before ``DoDragDrop`` *or* ``RegisterDragDrop`` — building the data
object on one thread and calling ``DoDragDrop`` on another without the second
thread having initialized OLE itself fails with ``CO_E_NOTINITIALIZED`` even
though the pointers are otherwise valid. In practice this is a non-issue here
since the window's own UI thread calls ``ensure_ole_initialized()`` once in
``open()``, before ``RegisterDragDrop``, and every ``begin_file_drag`` call
happens on that same thread (from inside its own mouse-move handling).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable

from ._win32_native import GUID, ComPtr

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

# --- shared GUIDs / HRESULTs / DROPEFFECT ------------------------------------

IID_IUnknown = GUID.from_str("00000000-0000-0000-C000-000000000046")
IID_IDropSource = GUID.from_str("00000121-0000-0000-C000-000000000046")
IID_IDropTarget = GUID.from_str("00000122-0000-0000-C000-000000000046")
IID_IDataObject = GUID.from_str("0000010e-0000-0000-C000-000000000046")
IID_IMarshal = GUID.from_str("00000003-0000-0000-C000-000000000046")

E_NOINTERFACE = -2147467262  # 0x80004002 as a signed HRESULT

DRAGDROP_S_DROP = 0x00040100
DRAGDROP_S_CANCEL = 0x00040101
DRAGDROP_S_USEDEFAULTCURSORS = 0x00040102

MK_LBUTTON = 0x0001

DROPEFFECT_NONE = 0
DROPEFFECT_COPY = 1
DROPEFFECT_MOVE = 2
DROPEFFECT_LINK = 4

_DROP_EFFECT_BY_OP = {"copy": DROPEFFECT_COPY, "move": DROPEFFECT_MOVE, "link": DROPEFFECT_LINK}
_OP_BY_DROP_EFFECT = {DROPEFFECT_COPY: "copy", DROPEFFECT_MOVE: "move", DROPEFFECT_LINK: "link"}

CF_HDROP = 15
DVASPECT_CONTENT = 1
TYMED_HGLOBAL = 1


def _guid_eq(a: GUID, b: GUID) -> bool:
    return bytes(a) == bytes(b)


ole32.OleInitialize.restype = ctypes.c_int32
ole32.OleInitialize.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.DoDragDrop.restype = ctypes.c_int32
ole32.DoDragDrop.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
ole32.RegisterDragDrop.restype = ctypes.c_int32
ole32.RegisterDragDrop.argtypes = [wintypes.HWND, ctypes.c_void_p]
ole32.RevokeDragDrop.restype = ctypes.c_int32
ole32.RevokeDragDrop.argtypes = [wintypes.HWND]
ole32.ReleaseStgMedium.argtypes = [ctypes.c_void_p]
ole32.CoCreateFreeThreadedMarshaler.restype = ctypes.c_int32
ole32.CoCreateFreeThreadedMarshaler.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

shell32.DragQueryFileW.restype = ctypes.c_uint32
shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_uint32]

_DRAGQUERY_FILE_COUNT = 0xFFFFFFFF

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

_ole_initialized = False


def ensure_ole_initialized() -> None:
    """``OleInitialize`` on the calling thread — required before
    ``DoDragDrop`` *and* ``RegisterDragDrop``. Idempotent per process; safe to
    call from more than one thread (each thread needs its own apartment, but
    every drag/drop call in this backend happens on the single UI thread)."""
    global _ole_initialized
    if not _ole_initialized:
        ole32.OleInitialize(None)
        _ole_initialized = True


class POINTL(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class FORMATETC(ctypes.Structure):
    _fields_ = [
        ("cfFormat", ctypes.c_ushort),
        ("ptd", ctypes.c_void_p),
        ("dwAspect", ctypes.c_uint32),
        ("lindex", ctypes.c_int32),
        ("tymed", ctypes.c_uint32),
    ]


class STGMEDIUM(ctypes.Structure):
    _fields_ = [
        ("tymed", ctypes.c_uint32),
        ("hGlobal", ctypes.c_void_p),  # the union member every format here uses
        ("pUnkForRelease", ctypes.c_void_p),
    ]


def _hdrop_formatetc() -> FORMATETC:
    return FORMATETC(CF_HDROP, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)


# IDataObject vtable indices (IUnknown[0-2], GetData[3], GetDataHere[4],
# QueryGetData[5], GetCanonicalFormatEtc[6], SetData[7], EnumFormatEtc[8],
# DAdvise[9], DUnadvise[10], EnumDAdvise[11]) — calling *into* an IDataObject
# OLE handed us (DragEnter/Drop's pDataObj), not one we authored, so this
# reuses the generic ComPtr.call the D2D/DWrite code already relies on.


def _data_object_offers_files(data_obj_addr: int) -> bool:
    obj = ComPtr(data_obj_addr)
    fmt = _hdrop_formatetc()
    hr = obj.call(5, ctypes.c_int32, [ctypes.POINTER(FORMATETC)], ctypes.byref(fmt))  # QueryGetData
    # QueryGetData returns S_OK (0) if the format is available, S_FALSE (1,
    # still >= 0) if not — `hr >= 0` alone would treat "not available" as a
    # yes and show a false "accept" cursor for a non-file drag.
    return hr == 0


def _data_object_hdrop_paths(data_obj_addr: int) -> list[str]:
    obj = ComPtr(data_obj_addr)
    fmt = _hdrop_formatetc()
    medium = STGMEDIUM()
    hr = obj.call(
        3, ctypes.c_int32, [ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM)], ctypes.byref(fmt), ctypes.byref(medium)
    )  # GetData
    if hr < 0 or not medium.hGlobal:
        return []
    try:
        hdrop = medium.hGlobal
        count = shell32.DragQueryFileW(hdrop, _DRAGQUERY_FILE_COUNT, None, 0)
        paths = []
        for i in range(count):
            length = shell32.DragQueryFileW(hdrop, i, None, 0)
            if length <= 0:
                continue
            buf = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(hdrop, i, buf, length + 1)
            paths.append(buf.value)
        return paths
    finally:
        ole32.ReleaseStgMedium(ctypes.byref(medium))


# --- IDropTarget (hand-built vtable): drop-in --------------------------------

_QI_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
_ADDREF_FUNC = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
_RELEASE_FUNC = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
_DRAG_ENTER_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, POINTL, ctypes.POINTER(ctypes.c_uint32)
)
_DRAG_OVER_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, POINTL, ctypes.POINTER(ctypes.c_uint32)
)
_DRAG_LEAVE_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
_DROP_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, POINTL, ctypes.POINTER(ctypes.c_uint32)
)


class _IDropTargetVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI_FUNC),
        ("AddRef", _ADDREF_FUNC),
        ("Release", _RELEASE_FUNC),
        ("DragEnter", _DRAG_ENTER_FUNC),
        ("DragOver", _DRAG_OVER_FUNC),
        ("DragLeave", _DRAG_LEAVE_FUNC),
        ("Drop", _DROP_FUNC),
    ]


class _IDropTargetObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IDropTargetVtbl))]


class DropTarget:
    """A live ``IDropTarget`` COM object registered on one ``hwnd`` via
    ``RegisterDragDrop``. ``on_drop(paths, (client_x, client_y))`` fires once,
    on an actual drop of a file-carrying data object; hovering with anything
    else shows the "no drop" cursor and never calls it. Keep the instance
    alive for as long as the window is registered (store it on the backend;
    see ``register_drop_target``) — same lifetime rule as ``DropSource``."""

    def __init__(self, on_drop: Callable[[list[str], tuple[int, int]], None]) -> None:
        self._on_drop = on_drop
        self._refcount = 1
        self._has_files = False
        self._vtbl = _IDropTargetVtbl(
            _QI_FUNC(self._query_interface),
            _ADDREF_FUNC(self._add_ref),
            _RELEASE_FUNC(self._release),
            _DRAG_ENTER_FUNC(self._drag_enter),
            _DRAG_OVER_FUNC(self._drag_over),
            _DRAG_LEAVE_FUNC(self._drag_leave),
            _DROP_FUNC(self._drop),
        )
        self._obj = _IDropTargetObj(ctypes.pointer(self._vtbl))
        self.addr = ctypes.addressof(self._obj)

    def _query_interface(self, this: int, riid, ppv) -> int:
        iid = riid[0]
        if _guid_eq(iid, IID_IUnknown) or _guid_eq(iid, IID_IDropTarget):
            ppv[0] = this
            self._refcount += 1
            return 0
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(self, this: int) -> int:
        self._refcount += 1
        return self._refcount

    def _release(self, this: int) -> int:
        self._refcount -= 1
        return max(self._refcount, 0)

    def _drag_enter(self, this: int, data_obj: int, grf_key_state: int, pt, pdw_effect) -> int:
        self._has_files = _data_object_offers_files(data_obj)
        pdw_effect[0] = DROPEFFECT_COPY if self._has_files else DROPEFFECT_NONE
        return 0

    def _drag_over(self, this: int, grf_key_state: int, pt, pdw_effect) -> int:
        pdw_effect[0] = DROPEFFECT_COPY if self._has_files else DROPEFFECT_NONE
        return 0

    def _drag_leave(self, this: int) -> int:
        self._has_files = False
        return 0

    def _drop(self, this: int, data_obj: int, grf_key_state: int, pt, pdw_effect) -> int:
        paths = _data_object_hdrop_paths(data_obj)
        pdw_effect[0] = DROPEFFECT_COPY if paths else DROPEFFECT_NONE
        self._has_files = False
        if paths:
            hwnd = _drop_target_hwnds.get(id(self), 0)
            screen_pt = wintypes.POINT(pt.x, pt.y)  # DragEnter/Over/Drop report screen coords
            user32.ScreenToClient(hwnd, ctypes.byref(screen_pt))
            self._on_drop(paths, (screen_pt.x, screen_pt.y))
        return 0


# DropTarget doesn't otherwise know its own hwnd (only the raw "this" COM
# pointer, which isn't the window handle) — register_drop_target records it
# here so _drop can convert the screen-coordinate drop point IDropTarget
# reports into client coordinates, matching every other mouse event.
_drop_target_hwnds: dict[int, int] = {}


def register_drop_target(hwnd: int, on_drop: Callable[[list[str], tuple[int, int]], None]) -> DropTarget:
    target = DropTarget(on_drop)
    _drop_target_hwnds[id(target)] = hwnd
    ole32.RegisterDragDrop(hwnd, target.addr)
    return target


def revoke_drop_target(hwnd: int, target: DropTarget) -> None:
    ole32.RevokeDragDrop(hwnd)
    _drop_target_hwnds.pop(id(target), None)


# --- IDropSource (hand-built vtable) + shell IDataObject: drag-out ----------

_QUERY_CONTINUE_DRAG_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, wintypes.BOOL, ctypes.c_uint32)
_GIVE_FEEDBACK_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32)


class _IDropSourceVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI_FUNC),
        ("AddRef", _ADDREF_FUNC),
        ("Release", _RELEASE_FUNC),
        ("QueryContinueDrag", _QUERY_CONTINUE_DRAG_FUNC),
        ("GiveFeedback", _GIVE_FEEDBACK_FUNC),
    ]


class _IDropSourceObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IDropSourceVtbl))]


class DropSource:
    """A live ``IDropSource`` COM object. ``addr`` is the raw interface
    pointer to hand ``DoDragDrop``; keep this instance alive (a plain local
    variable spanning the blocking ``DoDragDrop`` call is enough — see
    ``do_drag_drop``) since its vtable's function pointers close over this
    object's Python callables and both live only as long as this instance
    does."""

    def __init__(self) -> None:
        self._refcount = 1
        self._vtbl = _IDropSourceVtbl(
            _QI_FUNC(self._query_interface),
            _ADDREF_FUNC(self._add_ref),
            _RELEASE_FUNC(self._release),
            _QUERY_CONTINUE_DRAG_FUNC(self._query_continue_drag),
            _GIVE_FEEDBACK_FUNC(self._give_feedback),
        )
        self._obj = _IDropSourceObj(ctypes.pointer(self._vtbl))
        self.addr = ctypes.addressof(self._obj)

    def _query_interface(self, this: int, riid, ppv) -> int:
        iid = riid[0]
        if _guid_eq(iid, IID_IUnknown) or _guid_eq(iid, IID_IDropSource):
            ppv[0] = this
            self._refcount += 1
            return 0
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(self, this: int) -> int:
        self._refcount += 1
        return self._refcount

    def _release(self, this: int) -> int:
        self._refcount -= 1
        return max(self._refcount, 0)

    def _query_continue_drag(self, this: int, f_escape_pressed: int, grf_key_state: int) -> int:
        if f_escape_pressed:
            return DRAGDROP_S_CANCEL
        if not (grf_key_state & MK_LBUTTON):
            return DRAGDROP_S_DROP
        return 0  # S_OK: keep dragging

    def _give_feedback(self, this: int, dw_effect: int) -> int:
        return DRAGDROP_S_USEDEFAULTCURSORS  # let OLE draw the default drag cursors


# --- IDataObject (hand-built vtable) + a real CF_HDROP HGLOBAL: drag-out ----

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint32, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

GMEM_MOVEABLE = 0x0002


def _signed_hresult(value: int) -> int:
    return value - 0x1_0000_0000 if value >= 0x8000_0000 else value


E_NOTIMPL = _signed_hresult(0x80004001)
E_OUTOFMEMORY = _signed_hresult(0x8007000E)
DV_E_FORMATETC = _signed_hresult(0x80040064)
OLE_E_ADVISENOTSUPPORTED = _signed_hresult(0x80040003)


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", ctypes.c_uint32),  # offset, from this struct's start, of the file-name list
        ("pt", POINTL),
        ("fNC", ctypes.c_int32),  # BOOL
        ("fWide", ctypes.c_int32),  # BOOL: TRUE = UTF-16 names (what this module always writes)
    ]


_GET_DATA_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM)
)
_GET_DATA_HERE_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM)
)
_QUERY_GET_DATA_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(FORMATETC))
_GET_CANONICAL_FORMATETC_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(FORMATETC), ctypes.POINTER(FORMATETC)
)
_SET_DATA_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM), wintypes.BOOL
)
_ENUM_FORMATETC_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))
_DADVISE_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(FORMATETC), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
)
_DUNADVISE_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32)
_ENUM_DADVISE_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))


class _IDataObjectVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI_FUNC),
        ("AddRef", _ADDREF_FUNC),
        ("Release", _RELEASE_FUNC),
        ("GetData", _GET_DATA_FUNC),
        ("GetDataHere", _GET_DATA_HERE_FUNC),
        ("QueryGetData", _QUERY_GET_DATA_FUNC),
        ("GetCanonicalFormatEtc", _GET_CANONICAL_FORMATETC_FUNC),
        ("SetData", _SET_DATA_FUNC),
        ("EnumFormatEtc", _ENUM_FORMATETC_FUNC),
        ("DAdvise", _DADVISE_FUNC),
        ("DUnadvise", _DUNADVISE_FUNC),
        ("EnumDAdvise", _ENUM_DADVISE_FUNC),
    ]


class _IDataObjectObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IDataObjectVtbl))]


def _build_hdrop_hglobal(paths: list[str]) -> int:
    """A real CF_HDROP global memory block: a ``DROPFILES`` header (the
    classic 20-byte struct — file-list offset, drop point, ``fNC``, ``fWide``)
    followed by the paths as a double-null-terminated UTF-16 list, exactly the
    layout ``DragQueryFileW`` reads. Returns the HGLOBAL, or 0 on failure."""
    text = "\0".join(paths) + "\0\0"
    encoded = text.encode("utf-16-le")
    header_size = ctypes.sizeof(DROPFILES)
    total = header_size + len(encoded)
    hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, total)
    if not hglobal:
        return 0
    ptr = kernel32.GlobalLock(hglobal)
    if not ptr:
        return 0
    try:
        header = DROPFILES(header_size, POINTL(0, 0), 0, 1)  # fWide=TRUE
        ctypes.memmove(ptr, ctypes.byref(header), header_size)
        ctypes.memmove(ptr + header_size, encoded, len(encoded))
    finally:
        kernel32.GlobalUnlock(hglobal)
    return hglobal


def _matches_hdrop(fmt: "FORMATETC") -> bool:
    return fmt.cfFormat == CF_HDROP and fmt.dwAspect == DVASPECT_CONTENT and bool(fmt.tymed & TYMED_HGLOBAL)


# --- IEnumFORMATETC (hand-built vtable): lets EnumFormatEtc discover CF_HDROP -

IID_IEnumFORMATETC = GUID.from_str("00000103-0000-0000-C000-000000000046")
DATADIR_GET = 1

_NEXT_FUNC = ctypes.WINFUNCTYPE(
    ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(FORMATETC), ctypes.POINTER(ctypes.c_uint32)
)
_SKIP_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32)
_RESET_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)
_CLONE_FUNC = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))


class _IEnumFormatEtcVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QI_FUNC),
        ("AddRef", _ADDREF_FUNC),
        ("Release", _RELEASE_FUNC),
        ("Next", _NEXT_FUNC),
        ("Skip", _SKIP_FUNC),
        ("Reset", _RESET_FUNC),
        ("Clone", _CLONE_FUNC),
    ]


class _IEnumFormatEtcObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(_IEnumFormatEtcVtbl))]


class _HdropFormatEnumerator:
    """A single-entry ``IEnumFORMATETC`` yielding just CF_HDROP.

    Windows Explorer's shell drop target discovers what formats a *foreign*
    (non-shell) drag source offers by calling ``IDataObject::EnumFormatEtc``
    rather than probing ``QueryGetData``/``GetData`` for CF_HDROP directly —
    verified live: with ``EnumFormatEtc`` returning ``E_NOTIMPL`` (as every
    other unsupported ``IDataObject`` method here does), Explorer's
    DragEnter/DragOver never once attempted CF_HDROP itself and always
    settled on DROPEFFECT_NONE, while a same-process/single-thread consumer
    (an Electron app's drop target) that only ever calls QueryGetData/GetData
    directly worked fine regardless. Returning a real enumerator here — even
    a minimal one listing just the one format this data object actually
    supports — is what lets Explorer discover CF_HDROP at all."""

    def __init__(self) -> None:
        self._refcount = 1
        self._index = 0
        self._vtbl = _IEnumFormatEtcVtbl(
            _QI_FUNC(self._query_interface),
            _ADDREF_FUNC(self._add_ref),
            _RELEASE_FUNC(self._release),
            _NEXT_FUNC(self._next),
            _SKIP_FUNC(self._skip),
            _RESET_FUNC(self._reset),
            _CLONE_FUNC(self._clone),
        )
        self._obj = _IEnumFormatEtcObj(ctypes.pointer(self._vtbl))
        self.addr = ctypes.addressof(self._obj)

    def _query_interface(self, this: int, riid, ppv) -> int:
        iid = riid[0]
        if _guid_eq(iid, IID_IUnknown) or _guid_eq(iid, IID_IEnumFORMATETC):
            ppv[0] = this
            self._refcount += 1
            return 0
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(self, this: int) -> int:
        self._refcount += 1
        return self._refcount

    def _release(self, this: int) -> int:
        self._refcount -= 1
        return max(self._refcount, 0)

    def _next(self, this: int, celt: int, rgelt, pceltFetched) -> int:
        fetched = 0
        while fetched < celt and self._index < 1:
            rgelt[fetched] = _hdrop_formatetc()
            self._index += 1
            fetched += 1
        if pceltFetched:
            pceltFetched[0] = fetched
        return 0 if fetched == celt else 1  # S_OK / S_FALSE

    def _skip(self, this: int, celt: int) -> int:
        self._index = min(self._index + celt, 1)
        return 0

    def _reset(self, this: int) -> int:
        self._index = 0
        return 0

    def _clone(self, this: int, ppenum) -> int:
        clone = _HdropFormatEnumerator()
        clone._index = self._index
        _live_enumerators.append(clone)
        ppenum[0] = clone.addr
        return 0


# Keeps every enumerator (and its clones) alive for the life of the process —
# OLE holds the raw pointer, not a Python reference, and a drag session is
# short-lived enough that this never accumulates meaningfully.
_live_enumerators: list["_HdropFormatEnumerator"] = []


class FileDataObject:
    """A minimal, hand-built ``IDataObject`` exposing exactly one format —
    CF_HDROP over the given paths — as a real ``DoDragDrop`` data source.

    An earlier version of this module built the data object via the shell's
    ``SHCreateDataObject`` (given each path's PIDL) instead of authoring
    ``IDataObject`` by hand, to avoid a dozen-method COM interface. That
    turned out not to work in practice: ``SHCreateDataObject(NULL, cidl,
    apidl, ...)`` with *absolute* PIDLs and no parent folder — the
    NULL-``pidlFolder`` form its own docs describe as valid — produced an
    object whose ``QueryGetData``/``GetData`` both rejected CF_HDROP
    (verified live: ``QueryGetData`` returned S_FALSE, not S_OK, and
    ``EnumFormatEtc`` enumerated zero formats at all). Getting the real
    shell-aggregate object working would need splitting each path into a
    parent ``IShellFolder`` + child PIDL (``SHBindToParent``) and grouping by
    parent — real complexity for what CF_HDROP alone needs only two things
    from: a correctly-shaped ``DROPFILES`` global memory block (built by
    ``_build_hdrop_hglobal``) and a ``QueryGetData``/``GetData`` pair that
    recognize it. Only ``GetData`` and ``QueryGetData`` do real work here;
    every other ``IDataObject`` method returns ``E_NOTIMPL`` — a drop target
    that needs CF_HDROP only ever calls those two (verified against this
    backend's own ``IDropTarget``, and matches how minimal COM data-transfer
    objects are documented to behave: unsupported operations decline, they
    don't need to succeed)."""

    def __init__(self, paths: list[str]) -> None:
        self._paths = [str(p) for p in paths]
        self._refcount = 1
        self._vtbl = _IDataObjectVtbl(
            _QI_FUNC(self._query_interface),
            _ADDREF_FUNC(self._add_ref),
            _RELEASE_FUNC(self._release),
            _GET_DATA_FUNC(self._get_data),
            _GET_DATA_HERE_FUNC(self._get_data_here),
            _QUERY_GET_DATA_FUNC(self._query_get_data),
            _GET_CANONICAL_FORMATETC_FUNC(self._get_canonical_format_etc),
            _SET_DATA_FUNC(self._set_data),
            _ENUM_FORMATETC_FUNC(self._enum_format_etc),
            _DADVISE_FUNC(self._d_advise),
            _DUNADVISE_FUNC(self._d_unadvise),
            _ENUM_DADVISE_FUNC(self._enum_d_advise),
        )
        self._obj = _IDataObjectObj(ctypes.pointer(self._vtbl))
        self.addr = ctypes.addressof(self._obj)
        # Shell drop targets (verified live: Explorer's DragEnter) query this
        # object for IID_IMarshal — plausibly because their drag-drop handling
        # can touch the source's IDataObject from a different apartment/thread
        # than the one that started the drag. Aggregating the free-threaded
        # marshaler answers that so any apartment can reach the object
        # directly rather than needing a full proxy/stub round trip. (The
        # actual Explorer-drop-always-fails bug this project hit turned out to
        # be EnumFormatEtc returning E_NOTIMPL, not this — see
        # _HdropFormatEnumerator's docstring — but responding to IID_IMarshal
        # is correct COM practice for a drag source regardless.)
        self._marshal_unk = ctypes.c_void_p()
        hr = ole32.CoCreateFreeThreadedMarshaler(self.addr, ctypes.byref(self._marshal_unk))
        if hr != 0 or not self._marshal_unk:
            self._marshal_unk = None

    def _query_interface(self, this: int, riid, ppv) -> int:
        iid = riid[0]
        if _guid_eq(iid, IID_IUnknown) or _guid_eq(iid, IID_IDataObject):
            ppv[0] = this
            self._refcount += 1
            return 0
        if _guid_eq(iid, IID_IMarshal) and self._marshal_unk is not None:
            inner = ComPtr(self._marshal_unk.value)
            return inner.call(
                0, ctypes.c_int32, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(IID_IMarshal), ppv
            )
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(self, this: int) -> int:
        self._refcount += 1
        return self._refcount

    def _release(self, this: int) -> int:
        self._refcount -= 1
        return max(self._refcount, 0)

    def _query_get_data(self, this: int, pformatetc) -> int:
        return 0 if _matches_hdrop(pformatetc[0]) else 1  # S_OK / S_FALSE

    def _get_data(self, this: int, pformatetc, pmedium) -> int:
        if not _matches_hdrop(pformatetc[0]):
            return DV_E_FORMATETC
        hglobal = _build_hdrop_hglobal(self._paths)
        if not hglobal:
            return E_OUTOFMEMORY
        pmedium[0] = STGMEDIUM(TYMED_HGLOBAL, hglobal, None)
        return 0

    def _get_data_here(self, this: int, pformatetc, pmedium) -> int:
        return E_NOTIMPL

    def _get_canonical_format_etc(self, this: int, pformatetc_in, pformatetc_out) -> int:
        return E_NOTIMPL

    def _set_data(self, this: int, pformatetc, pmedium, f_release: int) -> int:
        return E_NOTIMPL

    def _enum_format_etc(self, this: int, direction: int, ppenum) -> int:
        if direction != DATADIR_GET:
            ppenum[0] = None
            return E_NOTIMPL
        enumerator = _HdropFormatEnumerator()
        _live_enumerators.append(enumerator)
        ppenum[0] = enumerator.addr
        return 0

    def _d_advise(self, this: int, pformatetc, advf: int, p_advise_sink, pdw_connection) -> int:
        return OLE_E_ADVISENOTSUPPORTED

    def _d_unadvise(self, this: int, connection: int) -> int:
        return E_NOTIMPL

    def _enum_d_advise(self, this: int, ppenum) -> int:
        ppenum[0] = None
        return OLE_E_ADVISENOTSUPPORTED


def create_file_data_object(paths: list[str]) -> FileDataObject | None:
    """A ``FileDataObject`` carrying ``paths`` — or ``None`` for an empty
    list, so the caller can treat "nothing to drag" uniformly."""
    if not paths:
        return None
    return FileDataObject(paths)


def drop_effects_mask(operations: tuple[str, ...]) -> int:
    mask = 0
    for op in operations:
        mask |= _DROP_EFFECT_BY_OP.get(op, 0)
    return mask or DROPEFFECT_COPY


def drop_effect_name(effect: int) -> str:
    return _OP_BY_DROP_EFFECT.get(effect, "none")


def do_drag_drop(data_object: FileDataObject, operations: tuple[str, ...]) -> str:
    """Run the (blocking) OLE drag session; returns the operation the
    receiver chose (``"copy"``/``"move"``/``"link"``/``"none"``)."""
    drop_source = DropSource()
    effect_out = ctypes.c_uint32(0)
    ole32.DoDragDrop(data_object.addr, drop_source.addr, drop_effects_mask(operations), ctypes.byref(effect_out))
    return drop_effect_name(effect_out.value)
