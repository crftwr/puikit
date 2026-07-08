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
  (suppressed) inline composition anchor to the live caret, in pixels. That
  alone does *not* move the candidate/conversion list — IMM32 treats it as a
  separate window, positioned via ``ImmSetCandidateWindow`` with
  ``CFS_CANDIDATEPOS`` — which is why both are set together here; skipping the
  second call leaves the candidate popup pinned at its IME-chosen default
  (observed: the bottom-right of the screen).
- **Inline preedit (A4).** ``WM_IME_SETCONTEXT`` is intercepted to clear the
  ``ISC_SHOWUICOMPOSITIONWINDOW`` bit (suppressing the OS's own floating
  composition box — the candidate/conversion popup is deliberately left alone,
  since a widget only draws the composition *string* inline, not a conversion
  list). ``WM_IME_COMPOSITION``'s ``GCS_COMPSTR``/``GCS_CURSORPOS`` are read via
  ``ImmGetCompositionStringW`` and turned into an ``IME_COMPOSITION`` event the
  same way ``setMarkedText:`` does on macOS. The handler for this message
  returns 0 (see ``WindowsBackend._handle_message``) instead of forwarding to
  ``DefWindowProc`` — which means Windows never gets the chance to synthesize
  ``WM_CHAR`` for a commit (``GCS_RESULTSTR``) the way it would for an
  untouched window. So a commit's result string is read here too and
  dispatched directly as key events, the same way macOS's ``insertText:``
  delivers committed characters.

The widget (``TextEdit``) re-asserts the composition window's position on
every draw, from wherever it currently thinks the caret is (A2). Two things
that position must NOT track are (a) the raw input cursor while typing kana
with nothing converted yet — it advances every keystroke, which would make
the window visibly crawl rightward as a word gets longer — and (b) must
track is the currently-selected *clause* once the user has converted a
multi-clause phrase and is cycling between clauses with left/right — each
clause has its own candidate list, so the window has to follow it. Both are
resolved by ``_target_clause_start``, which reads ``GCS_COMPATTR`` (one
attribute byte per composition character) for a run flagged
``ATTR_TARGET_CONVERTED``/``ATTR_TARGET_NOTCONVERTED``: absent during raw
typing (falls back to a fixed anchor at the composition's start — no
jitter), present and moving once a clause becomes the conversion target.
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
GCS_COMPATTR = 0x0010
GCS_RESULTSTR = 0x0800

# ATTR_* composition-character attributes (one byte per GCS_COMPSTR character,
# read via GCS_COMPATTR): which clause a character belongs to, and whether
# that clause is the one currently selected for conversion (the "target"
# clause a user cycles between with left/right during multi-segment
# conversion — Kanji conversion of a long phrase splits into several
# clauses, only one of which is being edited/converted at a time).
ATTR_TARGET_CONVERTED = 0x01
ATTR_TARGET_NOTCONVERTED = 0x03

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
CFS_CANDIDATEPOS = 0x0040


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class COMPOSITIONFORM(ctypes.Structure):
    _fields_ = [
        ("dwStyle", ctypes.c_uint32),
        ("ptCurrentPosition", POINT),
        ("rcArea", wintypes.RECT),
    ]


class CANDIDATEFORM(ctypes.Structure):
    _fields_ = [
        ("dwIndex", ctypes.c_uint32),
        ("dwStyle", ctypes.c_uint32),
        ("ptCurrentPosition", POINT),
        ("rcArea", wintypes.RECT),
    ]


imm32.ImmSetCompositionWindow.argtypes = [HIMC, ctypes.POINTER(COMPOSITIONFORM)]
imm32.ImmSetCandidateWindow.restype = wintypes.BOOL
imm32.ImmSetCandidateWindow.argtypes = [HIMC, ctypes.POINTER(CANDIDATEFORM)]


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
    """Move the composition anchor AND the candidate list to the live caret
    position, in client-area pixels. Both calls are needed: ImmSetCompositionWindow
    alone only repositions the (suppressed) inline composition box, not the
    separate candidate/conversion popup — without ImmSetCandidateWindow the
    IME leaves that window at its own default position."""
    himc = imm32.ImmGetContext(hwnd)
    if not himc:
        return
    try:
        cf = COMPOSITIONFORM(CFS_POINT, POINT(x_px, y_px), wintypes.RECT(0, 0, 0, 0))
        imm32.ImmSetCompositionWindow(himc, ctypes.byref(cf))
        candf = CANDIDATEFORM(0, CFS_CANDIDATEPOS, POINT(x_px, y_px), wintypes.RECT(0, 0, 0, 0))
        imm32.ImmSetCandidateWindow(himc, ctypes.byref(candf))
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


def _get_composition_attrs(himc: int) -> bytes:
    length = imm32.ImmGetCompositionStringW(himc, GCS_COMPATTR, None, 0)
    if length <= 0:
        return b""
    buf = ctypes.create_string_buffer(length)
    imm32.ImmGetCompositionStringW(himc, GCS_COMPATTR, buf, length)
    return buf.raw


def _target_clause_start(himc: int) -> int:
    """The character offset (into ``GCS_COMPSTR``) where the clause currently
    selected for conversion begins, or 0 if no clause is marked as the target
    — i.e. composition hasn't been converted yet and is just raw kana input,
    which has no distinguished clause (see ``read_composition``'s docstring)."""
    attrs = _get_composition_attrs(himc)
    for i, attr in enumerate(attrs):
        if attr in (ATTR_TARGET_CONVERTED, ATTR_TARGET_NOTCONVERTED):
            return i
    return 0


def read_composition(hwnd: int, lparam: int) -> tuple[str | None, int, int, str | None]:
    """Parse a ``WM_IME_COMPOSITION`` message: returns
    ``(preedit_text_or_None, cursor, target_start, result_text_or_None)``.

    ``preedit_text_or_None`` is the in-progress ``GCS_COMPSTR`` text (``None``
    if this message didn't carry one — the flag bit wasn't set, so any
    existing preedit the widget is showing should be left as-is).
    ``target_start`` is where the currently-selected clause begins (0 while
    there's no multi-clause conversion in progress) — see
    ``_target_clause_start``; the IME candidate window should anchor there,
    not to the (possibly still-advancing) input cursor, or it would jitter
    rightward as raw kana is typed instead of only moving when the user
    changes which clause they're converting.
    ``result_text_or_None`` is the committed ``GCS_RESULTSTR`` text when this
    message carried a commit (``None`` if it didn't, ``""`` if it did but the
    commit was empty). The caller must dispatch this text itself — Windows
    does not post ``WM_CHAR`` for it here, since ``DefWindowProc`` (the code
    path that would normally synthesize those) is never called for this
    message (see the module docstring)."""
    himc = imm32.ImmGetContext(hwnd)
    if not himc:
        return None, 0, 0, None
    try:
        result_text = _get_composition_string(himc, GCS_RESULTSTR) if (lparam & GCS_RESULTSTR) else None
        if not (lparam & GCS_COMPSTR):
            return None, 0, 0, result_text
        text = _get_composition_string(himc, GCS_COMPSTR)
        cursor = imm32.ImmGetCompositionStringW(himc, GCS_CURSORPOS, None, 0)
        target_start = _target_clause_start(himc)
        return text, max(cursor, 0), target_start, result_text
    finally:
        imm32.ImmReleaseContext(hwnd, himc)
