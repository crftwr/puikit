"""One keypress, every backend: the keyboard contract as a table.

Each row is a key the user presses; each column is what one backend receives
for it (window messages, an NSEvent's characters, console input records, tty
bytes, a DOM key) and the event the contract says it must turn into. The
per-backend tests check each translation on its own; this file is where the
backends are held side by side, so a key that arrives as two events on one of
them (xefm#760) or under another name shows up as one row disagreeing.

Raw inputs come from each platform's documented delivery, not from a recording
yet: Windows GUI from the WM_KEYDOWN -> WM_CHAR pair TranslateMessage queues,
a POSIX tty from the bytes xterm sends in its default mode. Replace a row's raw
input with a recording when one is taken, and name the environment it came from.
The Windows console column IS a recording, from ReadConsoleInputW under both
Windows Terminal and the classic console window, which agreed on every row. A
pasted newline arrived as the Enter row's record, except a lone LF pasted into
the classic window, which arrived as Ctrl+Enter's.

Ctrl+I/J/M/H is where GUI and TUI part (contract §4a): a GUI sees the letter
key, a terminal only the control byte, which is Tab / Enter / Backspace's own.
Those two hosts cross to the GUI side: their records name the letter key, and
the console reads them that way there (``win_term``, from the same records).
"""

import io
import sys

import pytest

from puikit.backends._vt_input import parse_vt_input
from puikit.backends.curses_backend import CursesBackend
from puikit.backends.vt_backend import VTBackend, _StreamConsole, _win_key_record
from puikit.backends.web_backend import translate_key as web_translate_key

CTRL = frozenset({"ctrl"})
NONE = frozenset()

# Windows virtual keys and console control-key state used below.
VK_BACK, VK_TAB, VK_RETURN, VK_ESCAPE = 0x08, 0x09, 0x0D, 0x1B
LEFT_CTRL = 0x0008

# key -> {surface: raw input}. Surfaces:
#   win_gui   (vk, wm_char, mods)          WM_KEYDOWN then the WM_CHAR it queues
#   macos     (charactersIgnoringModifiers, mods)
#   web       (KeyboardEvent.key, mods)
#   win_con   (UnicodeChar, vk, dwControlKeyState)
#   win_term  the win_con records, read as under those hosts (no column)
#   posix     the bytes a tty delivers
#   curses    what get_wch() returns
RAW = {
    "Ctrl+J": dict(win_gui=(ord("J"), 0x0A, CTRL), macos=("j", CTRL), web=("j", CTRL),
                   win_con=("\n", ord("J"), LEFT_CTRL), posix="\n", curses="\n"),
    "Ctrl+M": dict(win_gui=(ord("M"), 0x0D, CTRL), macos=("m", CTRL), web=("m", CTRL),
                   win_con=("\r", ord("M"), LEFT_CTRL), posix="\r", curses="\r"),
    "Ctrl+I": dict(win_gui=(ord("I"), 0x09, CTRL), macos=("i", CTRL), web=("i", CTRL),
                   win_con=("\t", ord("I"), LEFT_CTRL), posix="\t", curses="\t"),
    "Ctrl+H": dict(win_gui=(ord("H"), 0x08, CTRL), macos=("h", CTRL), web=("h", CTRL),
                   win_con=("\x08", ord("H"), LEFT_CTRL), posix="\x08", curses="\x08"),
    "Ctrl+A": dict(win_gui=(ord("A"), 0x01, CTRL), macos=("a", CTRL), web=("a", CTRL),
                   win_con=("\x01", ord("A"), LEFT_CTRL), posix="\x01", curses="\x01"),
    "Enter": dict(win_gui=(VK_RETURN, 0x0D, NONE), macos=("\r", NONE), web=("Enter", NONE),
                  win_con=("\r", VK_RETURN, 0), posix="\r", curses="\r"),
    "Ctrl+Enter": dict(win_gui=(VK_RETURN, 0x0A, CTRL), macos=("\r", CTRL), web=("Enter", CTRL),
                       win_con=("\n", VK_RETURN, LEFT_CTRL), posix="\r", curses="\r"),
    "Tab": dict(win_gui=(VK_TAB, 0x09, NONE), macos=("\t", NONE), web=("Tab", NONE),
                win_con=("\t", VK_TAB, 0), posix="\t", curses="\t"),
    "Backspace": dict(win_gui=(VK_BACK, 0x08, NONE), macos=("\x7f", NONE), web=("Backspace", NONE),
                      win_con=("\x08", VK_BACK, 0), posix="\x7f", curses="\x7f"),
    "Escape": dict(win_gui=(VK_ESCAPE, 0x1B, NONE), macos=("\x1b", NONE), web=("Escape", NONE),
                   win_con=("\x1b", VK_ESCAPE, 0), posix="\x1b", curses="\x1b"),
    "a": dict(win_gui=(ord("A"), ord("a"), NONE), macos=("a", NONE), web=("a", NONE),
              win_con=("a", ord("A"), 0), posix="a", curses="a"),
}

GUI = ("win_gui", "macos", "web")
TUI = ("win_con", "posix", "curses")

# key -> the (key, modifiers) every GUI surface delivers, and every TUI one.
# A TUI's modifiers are what it can detect: the Windows console reports Ctrl,
# a tty cannot (§1), so those two are given per surface.
EXPECTED = {
    "Ctrl+J": {"gui": ("j", CTRL), "win_con": ("enter", CTRL), "tty": ("enter", NONE)},
    "Ctrl+M": {"gui": ("m", CTRL), "win_con": ("enter", CTRL), "tty": ("enter", NONE)},
    "Ctrl+I": {"gui": ("i", CTRL), "win_con": ("tab", CTRL), "tty": ("tab", NONE)},
    "Ctrl+H": {"gui": ("h", CTRL), "win_con": ("backspace", CTRL), "tty": ("backspace", NONE)},
    "Ctrl+A": {"gui": ("a", CTRL), "win_con": ("a", CTRL), "tty": ("a", CTRL)},
    "Enter": {"gui": ("enter", NONE), "win_con": ("enter", NONE), "tty": ("enter", NONE)},
    # A tty sends CR for Ctrl+Enter, the same as Enter: the Ctrl is lost there.
    "Ctrl+Enter": {"gui": ("enter", CTRL), "win_con": ("enter", CTRL), "tty": ("enter", NONE)},
    "Tab": {"gui": ("tab", NONE), "win_con": ("tab", NONE), "tty": ("tab", NONE)},
    "Backspace": {"gui": ("backspace", NONE), "win_con": ("backspace", NONE), "tty": ("backspace", NONE)},
    "Escape": {"gui": ("escape", NONE), "win_con": ("escape", NONE), "tty": ("escape", NONE)},
    "a": {"gui": ("a", NONE), "win_con": ("a", NONE), "tty": ("a", NONE)},
}


def _expected(key, surface):
    row = EXPECTED[key]
    if surface in GUI or surface == "win_term":
        return row["gui"]
    if surface == "win_con":
        return row["win_con"]
    return row["tty"]


# --- surfaces: raw input -> the KEY events the backend dispatches -----------


class _Console(_StreamConsole):
    def __init__(self):
        super().__init__(stream=io.StringIO(), size=(40, 10))
        self.queue = []

    def size(self):
        return (40, 10)

    def read_input(self, timeout_ms):
        return self.queue.pop(0) if self.queue else []


def _through_vt_engine(records):
    con = _Console()
    be = VTBackend(console=con)
    be.open()
    try:
        con.queue.append(records)
        events = []
        be.run_event_loop_iteration(events.append, 0)
        return events
    finally:
        be.close()


def _win_con(raw):
    char, vk, control = raw
    return _through_vt_engine([_win_key_record(char, vk, control)])


def _win_term(raw):
    char, vk, control = raw
    return _through_vt_engine([_win_key_record(char, vk, control, ctrl_letters=True)])


def _posix(raw):
    records, rest = parse_vt_input(raw, flush=True)
    assert rest == ""
    return _through_vt_engine(records)


def _curses(raw):
    return [CursesBackend()._translate_char(raw)]


def _web(raw):
    key, mods = raw
    return [web_translate_key(key, {m: True for m in mods})]


def _macos(raw):
    macos_backend = pytest.importorskip("puikit.backends.macos_backend")
    chars, mods = raw
    flags = macos_backend.NSEventModifierFlagControl if "ctrl" in mods else 0
    return [macos_backend.translate_key(chars, flags)]


def _win_gui(raw):
    if sys.platform != "win32":
        pytest.skip("Windows GUI: needs a real window and message queue")
    import ctypes
    from ctypes import wintypes

    from puikit.backends import _win32_native as native
    from puikit.backends import windows_backend
    from puikit.backends.windows_backend import WindowsBackend

    vk, wm_char, mods = raw
    patch = pytest.MonkeyPatch()
    patch.setattr(windows_backend, "_key_modifiers", lambda: mods)
    backend = WindowsBackend(width=40, height=12, title="puikit-conformance")
    backend.open()
    events = []
    backend._handler = events.append
    try:
        hwnd = backend._hwnd
        # TranslateMessage queues the WM_CHAR before the keydown is dispatched.
        native.user32.PostMessageW(hwnd, native.WM_CHAR, wm_char, 0)
        backend._handle_message(hwnd, native.WM_KEYDOWN, vk, 0)
        msg = wintypes.MSG()
        while native.user32.PeekMessageW(ctypes.byref(msg), hwnd, native.WM_CHAR,
                                         native.WM_CHAR, native.PM_REMOVE):
            backend._handle_message(hwnd, msg.message, msg.wParam, msg.lParam)
        return events
    finally:
        backend._handler = None
        backend.close()
        patch.undo()


SURFACES = {
    "win_gui": _win_gui, "macos": _macos, "web": _web,
    "win_con": _win_con, "win_term": _win_term, "posix": _posix, "curses": _curses,
}
# A surface that reads another one's raw input.
RAW_OF = {"win_term": "win_con"}


@pytest.mark.parametrize("surface", list(SURFACES))
@pytest.mark.parametrize("key", list(RAW))
def test_one_keypress_is_one_contract_event(key, surface):
    raw = RAW[key][RAW_OF.get(surface, surface)]
    events = [e for e in SURFACES[surface](raw) if e is not None]
    assert [(e.key, e.modifiers) for e in events] == [_expected(key, surface)]


def test_ctrl_letter_keys_capability_matches_the_table():
    # The capability is how an application learns which column it is in, so
    # it must agree with what the Ctrl+J row delivers.
    from puikit.backends.web_backend import PROFILE_WEB

    assert PROFILE_WEB.supports("ctrl_letter_keys")
    assert not VTBackend(console=_Console()).capabilities.supports("ctrl_letter_keys")
    terminal = _Console()
    terminal.ctrl_letters = True   # what _WindowsConsole sets under Windows Terminal
    assert VTBackend(console=terminal).capabilities.supports("ctrl_letter_keys")
    assert not CursesBackend().capabilities.supports("ctrl_letter_keys")
    if sys.platform == "win32":
        from puikit.backends.windows_backend import WindowsBackend
        assert WindowsBackend.PROFILE.supports("ctrl_letter_keys")
    if sys.platform == "darwin":
        from puikit.backends.macos_backend import MacOSBackend
        assert MacOSBackend.PROFILE.supports("ctrl_letter_keys")


def test_every_row_covers_every_surface():
    for key, row in RAW.items():
        assert set(row) == set(SURFACES) - set(RAW_OF), key
        assert key in EXPECTED


# --- Windows GUI: Ctrl / Alt on a punctuation key ---------------------------
#
# Windows types a control code (Ctrl+- is 0x1F, Ctrl+[ is 0x1B) or nothing
# (Ctrl+=) for these, so the keydown reports the glyph the key types without
# Ctrl and Alt. The decision is in _win32_keys and runs anywhere; the message
# handling below it needs a real window.

ALT = frozenset({"alt"})
CTRL_ALT = frozenset({"ctrl", "alt"})
CTRL_SHIFT = frozenset({"ctrl", "shift"})


def _layout(plain, shifted=None, altgr=None):
    """A stand-in for ToUnicodeEx on one key: what it types without Ctrl/Alt
    (Shift applied by the caller), and with Ctrl+Alt held (AltGr)."""
    def translate(strip):
        return plain if strip else altgr
    return translate


@pytest.mark.parametrize("mods, plain, expected", [
    (CTRL, "-", "-"),                  # Ctrl+-  (WM_CHAR 0x1F)
    (CTRL, "=", "="),                  # Ctrl+=  (no WM_CHAR)
    (CTRL_SHIFT, "+", "+"),            # Ctrl+Shift+= on US is Ctrl and "+"
    (CTRL, "\\", "\\"),                # Ctrl+\  (WM_CHAR 0x1C)
    (CTRL, "[", "["),                  # Ctrl+[  (WM_CHAR 0x1B, not Escape)
    (ALT, "+", "+"),                   # Alt+Num+ / Alt+Shift+=
    (CTRL_ALT, "-", "-"),              # Ctrl+Alt+- that types nothing is a chord
])
def test_ctrl_alt_punctuation_reports_its_glyph(mods, plain, expected):
    from puikit.backends._win32_keys import chord_glyph
    assert chord_glyph(mods, _layout(plain)) == expected


def test_altgr_that_types_a_character_is_left_to_wm_char():
    # German AltGr+Q is "@": text, not a chord.
    from puikit.backends._win32_keys import chord_glyph
    assert chord_glyph(CTRL_ALT, _layout("q", altgr="@")) is None


@pytest.mark.parametrize("mods, plain", [
    (NONE, "-"),                       # no Ctrl/Alt: WM_CHAR types it
    (frozenset({"shift"}), "+"),
    (CTRL, None),                      # a dead key, or nothing
    (CTRL, " "),                       # Space
    (CTRL, "\r"),                      # Ctrl+Enter keeps its own path
    (CTRL, "ab"),                      # more than one character
])
def test_what_is_not_a_punctuation_chord(mods, plain):
    from puikit.backends._win32_keys import chord_glyph
    assert chord_glyph(mods, _layout(plain)) is None


def _win_chord(vk, mods, queued, typed):
    """Drive one keydown through a real WindowsBackend with ``queued`` posted
    first, as TranslateMessage does, and ToUnicodeEx answered by ``typed``."""
    if sys.platform != "win32":
        pytest.skip("Windows GUI: needs a real window and message queue")
    import ctypes
    from ctypes import wintypes

    from puikit.backends import _win32_native as native
    from puikit.backends import windows_backend
    from puikit.backends.windows_backend import WindowsBackend

    patch = pytest.MonkeyPatch()
    patch.setattr(windows_backend, "_key_modifiers", lambda: mods)
    patch.setattr(windows_backend, "_typed_text", lambda vk, lparam, strip: typed(strip))
    backend = WindowsBackend(width=40, height=12, title="puikit-conformance")
    backend.open()
    events = []
    backend._handler = events.append
    try:
        hwnd = backend._hwnd
        for msg, code in queued:
            native.user32.PostMessageW(hwnd, msg, code, 0)
        down = native.WM_SYSKEYDOWN if "alt" in mods else native.WM_KEYDOWN
        backend._handle_message(hwnd, down, vk, 0)
        msg = wintypes.MSG()
        left = []
        while native.user32.PeekMessageW(ctypes.byref(msg), hwnd, native.WM_CHAR,
                                         native.WM_SYSCHAR, native.PM_REMOVE):
            if msg.message in (native.WM_CHAR, native.WM_SYSCHAR):
                left.append((msg.message, msg.wParam))
                backend._handle_message(hwnd, msg.message, msg.wParam, msg.lParam)
        return [(e.key, e.char, e.modifiers) for e in events], left
    finally:
        backend._handler = None
        backend.close()
        patch.undo()


VK_OEM_MINUS, VK_OEM_PLUS, VK_OEM_4 = 0xBD, 0xBB, 0xDB
_needs_windows = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows GUI: needs a real window and message queue")


@_needs_windows
def test_win_ctrl_minus_is_one_event_and_its_control_code_is_dropped():
    from puikit.backends import _win32_native as native
    events, left = _win_chord(VK_OEM_MINUS, CTRL, [(native.WM_CHAR, 0x1F)],
                              _layout("-"))
    assert events == [("-", "-", CTRL)] and left == []


@_needs_windows
def test_win_ctrl_bracket_is_no_longer_escape():
    from puikit.backends import _win32_native as native
    events, left = _win_chord(VK_OEM_4, CTRL, [(native.WM_CHAR, 0x1B)], _layout("["))
    assert events == [("[", "[", CTRL)] and left == []


@_needs_windows
def test_win_ctrl_shift_equal_is_ctrl_plus():
    events, _left = _win_chord(VK_OEM_PLUS, CTRL_SHIFT, [], _layout("+"))
    assert events == [("+", "+", CTRL)]


@_needs_windows
def test_win_alt_plus_drops_its_syschar():
    from puikit.backends import _win32_native as native
    events, left = _win_chord(VK_OEM_PLUS, frozenset({"alt", "shift"}),
                              [(native.WM_SYSCHAR, ord("+"))], _layout("+"))
    assert events == [("+", "+", ALT)] and left == []


@_needs_windows
def test_win_altgr_still_types_through_wm_char():
    from puikit.backends import _win32_native as native
    events, left = _win_chord(ord("Q"), CTRL_ALT, [(native.WM_CHAR, ord("@"))],
                              _layout("q", altgr="@"))
    # The letter path is untouched: Ctrl+Alt+Q still reports "q", and the "@"
    # WM_CHAR stays for _on_char, as it did before.
    assert left == [(native.WM_CHAR, ord("@"))]
