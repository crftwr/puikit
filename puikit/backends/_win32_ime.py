"""IMM32 (Input Method Editor) support for the Windows backend.

Mirrors the macOS backend's IME contract (see ``MacOSBackend``/``_PuiKitView``'s
``NSTextInputClient`` implementation): mode-gated so a CJK input source never
swallows a command-mode single-letter binding (``j``/``f``/``v``) into
composition, positioned at the focused field's caret, and rendered inline
(the widget draws the underlined preedit string itself from
``IME_COMPOSITION`` events) rather than through the OS's own floating
composition box.

Three IMM32 mechanisms, each verified live against a real window before this
module was wired into ``WindowsBackend``:

- **Mode gate (A1).** ``ImmAssociateContext(hwnd, NULL)`` fully detaches the
  window's input context — the standard technique games use to disable IME
  outright — so physical keys pass straight through as plain ``WM_KEYDOWN`` /
  ``WM_CHAR`` with no composition, letting a bare ``j`` dispatch as a command
  even with a Japanese IME selected. ``ImmAssociateContext(hwnd, himc)``
  re-attaches the same context to re-enable it. The context handle returned by
  the first detach *is* the window's default context (verified: re-associating
  it round-trips ``ImmGetContext`` back to the original handle), so one saved
  handle suffices for the window's lifetime.
- **Position (A2).** ``ImmSetCompositionWindow`` with ``CFS_POINT`` moves the
  candidate/composition anchor to the live caret (in pixels) instead of the
  window corner.
- **Inline preedit (A4).** ``WM_IME_SETCONTEXT`` is intercepted to clear the
  ``ISC_SHOWUICOMPOSITIONWINDOW`` bit (suppressing the OS's own floating
  composition box — the candidate/conversion popup is deliberately left alone,
  since a widget only draws the composition *string* inline, not a conversion
  list). ``WM_IME_COMPOSITION``'s ``GCS_COMPSTR``/``GCS_CURSORPOS`` are read via
  ``ImmGetCompositionStringW`` and turned into an ``IME_COMPOSITION`` event the
  same way ``setMarkedText:`` does on macOS. A commit (``GCS_RESULTSTR``) only
  clears the preedit here — the committed characters themselves still arrive
  through the existing ``WM_CHAR`` path (unlike macOS, which has no separate
  commit message), so they are not re-dispatched from this module or every
  IME-committed character would be inserted twice.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
imm32 = ctypes.WinDLL("imm32", use_last_error=True)

HIMC = ctypes.c_void_p

imm32.ImmGetContext.restype = HIMC
imm32.ImmGetContext.argtypes = [wintypes.HWND]
imm32.ImmReleaseContext.restype = wintypes.BOOL
imm32.ImmReleaseContext.argtypes = [wintypes.HWND, HIMC]
imm32.ImmAssociateContext.restype = HIMC
imm32.ImmAssociateContext.argtypes = [wintypes.HWND, HIMC]
imm32.ImmGetCompositionStringW.restype = ctypes.c_long
imm32.ImmGetCompositionStringW.argtypes = [HIMC, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
imm32.ImmSetCompositionWindow.restype = wintypes.BOOL
imm32.ImmNotifyIME.restype = wintypes.BOOL
imm32.ImmNotifyIME.argtypes = [HIMC, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]

# WM_IME_* messages (winuser.h) — not in _win32_native's WM_* list since only
# this module's handlers reference them.
WM_IME_SETCONTEXT = 0x0281
WM_IME_STARTCOMPOSITION = 0x010D
WM_IME_COMPOSITION = 0x010F
WM_IME_ENDCOMPOSITION = 0x010E

# GCS_* composition-string attribute flags (ImmGetCompositionStringW's dwIndex).
GCS_COMPSTR = 0x0008
GCS_CURSORPOS = 0x0080
GCS_RESULTSTR = 0x0800

# ISC_SHOWUICOMPOSITIONWINDOW (WM_IME_SETCONTEXT's lParam, the ISC_* family):
# cleared so the OS does not draw its own floating composition box — the
# widget renders the preedit string inline instead (matches macOS, which has
# no separate OS composition window to suppress).
ISC_SHOWUICOMPOSITIONWINDOW = 0x80000000

# NI_COMPOSITIONSTR (ImmNotifyIME's dwAction) + CPS_CANCEL (its dwIndex):
# cancels an in-progress composition outright (end_text_input's cleanup, the
# IMM32 analogue of macOS's discardMarkedText).
NI_COMPOSITIONSTR = 0x0015
CPS_CANCEL = 0x0004

CFS_POINT = 0x0002


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class COMPOSITIONFORM(ctypes.Structure):
    _fields_ = [
        ("dwStyle", ctypes.c_uint32),
        ("ptCurrentPosition", POINT),
        ("rcArea", wintypes.RECT),
    ]


imm32.ImmSetCompositionWindow.argtypes = [HIMC, ctypes.POINTER(COMPOSITIONFORM)]


def disable_ime(hwnd: int) -> int:
    """Detach ``hwnd``'s input context (command mode); returns the context
    handle that was attached (the window's default context — save it to
    restore with :func:`enable_ime` later)."""
    return imm32.ImmAssociateContext(hwnd, None) or 0


def enable_ime(hwnd: int, himc: int) -> None:
    """Re-attach the context saved by :func:`disable_ime` (text mode)."""
    if himc:
        imm32.ImmAssociateContext(hwnd, HIMC(himc))


def cancel_composition(hwnd: int) -> None:
    """Cancel any in-progress composition (focus leaving the field mid-IME)."""
    himc = imm32.ImmGetContext(hwnd)
    if not himc:
        return
    try:
        imm32.ImmNotifyIME(himc, NI_COMPOSITIONSTR, CPS_CANCEL, 0)
    finally:
        imm32.ImmReleaseContext(hwnd, himc)


def set_composition_position(hwnd: int, x_px: int, y_px: int) -> None:
    """Move the candidate/composition anchor to the live caret position, in
    client-area pixels."""
    himc = imm32.ImmGetContext(hwnd)
    if not himc:
        return
    try:
        cf = COMPOSITIONFORM(CFS_POINT, POINT(x_px, y_px), wintypes.RECT(0, 0, 0, 0))
        imm32.ImmSetCompositionWindow(himc, ctypes.byref(cf))
    finally:
        imm32.ImmReleaseContext(hwnd, himc)


def strip_show_composition_window(lparam: int) -> int:
    """``WM_IME_SETCONTEXT``'s lParam with ``ISC_SHOWUICOMPOSITIONWINDOW``
    cleared, so ``DefWindowProc`` won't draw the default composition box."""
    return lparam & ~ISC_SHOWUICOMPOSITIONWINDOW


def _get_composition_string(himc: int, index: int) -> str:
    length = imm32.ImmGetCompositionStringW(himc, index, None, 0)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length // 2 + 1)
    imm32.ImmGetCompositionStringW(himc, index, buf, length)
    return buf.value


def read_composition(hwnd: int, lparam: int) -> tuple[str | None, int, bool]:
    """Parse a ``WM_IME_COMPOSITION`` message: returns
    ``(preedit_text_or_None, cursor, has_result)``.

    ``preedit_text_or_None`` is the in-progress ``GCS_COMPSTR`` text (``None``
    if this message didn't carry one — the flag bit wasn't set, so any
    existing preedit the widget is showing should be left as-is). ``has_result``
    is True when a ``GCS_RESULTSTR`` (commit) accompanied this message; the
    committed characters themselves are not returned here — they arrive
    through the ordinary ``WM_CHAR`` messages Windows posts right after this
    one, so the caller only needs to know to clear the preedit, not to insert
    text itself (see the module docstring)."""
    himc = imm32.ImmGetContext(hwnd)
    if not himc:
        return None, 0, False
    try:
        has_result = bool(lparam & GCS_RESULTSTR)
        if not (lparam & GCS_COMPSTR):
            return None, 0, has_result
        text = _get_composition_string(himc, GCS_COMPSTR)
        cursor = imm32.ImmGetCompositionStringW(himc, GCS_CURSORPOS, None, 0)
        return text, max(cursor, 0), has_result
    finally:
        imm32.ImmReleaseContext(hwnd, himc)
