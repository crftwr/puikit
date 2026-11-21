"""Which glyph a Ctrl / Alt chord on a punctuation key stands for (Windows GUI).

Windows reports a key twice: WM_KEYDOWN names the physical key, and the
WM_CHAR (WM_SYSCHAR under Alt) that TranslateMessage queues carries what it
typed. With Ctrl or Alt held, that second message is no use for a punctuation
key: Ctrl+- types the control code 0x1F, Ctrl+[ types 0x1B (Escape's own),
Ctrl+= types nothing at all, and Alt+key arrives as a WM_SYSCHAR the backend
never read. So the keydown asks the layout what the key types *without* Ctrl
and Alt (Shift kept: Ctrl+Shift+= is Ctrl and "+") and reports that glyph with
the chord, as macOS does for Cmd+-.

Kept apart from ``windows_backend`` so the decision can be tested on any OS:
``translate`` stands for ToUnicodeEx and is the only thing that touches Win32.
"""

from __future__ import annotations

from collections.abc import Callable


#: VK_OEM_AUTO / VK_OEM_ENLW: the Japanese IME's 半角/全角 toggle.
_IME_MODE_VKS = frozenset({0xF3, 0xF4})


def chord_vk(vk: int, mods: frozenset[str], scan_vk: Callable[[], int]) -> int:
    """The virtual key to read a Ctrl / Alt chord as.

    The Japanese layout on a US (101) keyboard reports Ctrl+` and Alt+` not as
    VK_OEM_3 but as the IME's 半角/全角 toggle (VK_OEM_AUTO and VK_OEM_ENLW,
    alternately), which types nothing, so the chord was lost. ``scan_vk()``
    returns the virtual key the physical key's scan code stands for under the
    layout (MapVirtualKeyEx); when that is a different, ordinary key, the chord
    is that key's. On a JIS keyboard the scan code *is* 半角/全角, so it maps
    back to the toggle and the key is left alone.
    """
    if vk not in _IME_MODE_VKS or not {"ctrl", "alt"} & mods:
        return vk
    real = scan_vk()
    return real if real and real not in _IME_MODE_VKS else vk


def chord_glyph(mods: frozenset[str],
                translate: Callable[[bool], str | None]) -> str | None:
    """The glyph to report for a keydown held with ``mods``, or None to leave
    the key to the existing path.

    ``translate(strip)`` returns what the key types under the current layout
    and keyboard state, with Ctrl and Alt released when ``strip`` is True, or
    None when it types nothing, a dead key or more than one character.

    - No Ctrl and no Alt: None (a plain key types through WM_CHAR).
    - Ctrl and Alt together that still type a printable character are AltGr
      (``@`` on a German layout): text, not a chord, so None and WM_CHAR types
      it as before.
    - Otherwise the glyph the key types without Ctrl and Alt, when that is one
      printable, non-space character; None for anything else (the modifier
      keys themselves, Space, a dead key).
    """
    if not {"ctrl", "alt"} & mods:
        return None
    if {"ctrl", "alt"} <= mods and _printable(translate(False)):
        return None
    glyph = translate(True)
    return glyph if _printable(glyph) else None


def _printable(text: str | None) -> bool:
    return (text is not None and len(text) == 1 and text != " "
            and text.isprintable())
