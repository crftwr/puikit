"""Decoding the VT escape sequences a terminal writes on stdin.

Two backends read a raw VT input stream and must decode it themselves: the
curses backend, whose macOS ncurses cannot decode SGR mouse modes 1002/1006 and
so drives mouse tracking and parses the reports by hand, and the VT backend's
POSIX console, which owns the tty outright and sees *every* key as raw bytes.
This module is the single copy both feed — moved out of curses_backend so the
VT backend does not import curses to share a table.

Two kinds of consumer, two shapes of result:

* The curses backend feeds one already-collected sequence at a time into
  :func:`_parse_csi_key` / :func:`_meta_char_event` and gets an ``Event``.
* The POSIX console feeds the whole decoded stream into
  :func:`parse_vt_input` and gets backend *input records* — the dicts the VT
  engine's event loop consumes — plus the unconsumed tail of a sequence still
  arriving.

The curses path only ever sees sequences ncurses did NOT pre-assemble into
keycodes, so the F-key and Shift+Tab entries below are additive there; on the
POSIX console they are the only way those keys exist at all. Both also read
``CSI <code> ; <mod> u`` (:func:`_parse_csi_u`), the form some terminals send
for a chord the legacy bytes cannot carry (Ctrl+` from Ghostty).
"""

from __future__ import annotations

import re

from ..event import Event, EventType, char_key_event, is_typed_char

# --- key tables ---------------------------------------------------------------
#
# Modern terminals report a modified cursor/edit key as an xterm CSI sequence:
# ``ESC [ 1 ; <mod> <final>`` for arrows / home / end / F1-F4, or
# ``ESC [ <n> ; <mod> ~`` for delete / insert / page / F5-F12 keys. ``<mod>`` is
# ``1 + bitmask`` where the bits are Shift=1, Alt=2, Ctrl=4, Meta=8 (so Ctrl =
# 5, Alt = 3, Shift+Ctrl = 6). The same finals arrive SS3-encoded (``ESC O C``,
# ``ESC O P``) in application-cursor mode.

#: CSI/SS3 final byte (letter form) -> key name — arrows, home/end, F1-F4.
_CSI_FINAL_KEYS = {
    "A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
    "P": "f1", "Q": "f2", "R": "f3", "S": "f4",
}
#: Leading number of a ``CSI <n> ; <mod> ~`` sequence -> key name.
_CSI_TILDE_KEYS = {
    "1": "home", "2": "insert", "3": "delete", "4": "end",
    "5": "pageup", "6": "pagedown", "7": "home", "8": "end",
    "11": "f1", "12": "f2", "13": "f3", "14": "f4", "15": "f5",
    "17": "f6", "18": "f7", "19": "f8", "20": "f9", "21": "f10",
    "23": "f11", "24": "f12",
}
#: ESC-prefixed meta char -> key name (readline word-editing: Alt+b / Alt+f
#: move by word, Alt+d deletes the next word; Alt+Backspace is handled apart).
_META_WORD_KEYS = {"b": "left", "f": "right", "d": "delete"}

# SGR mouse button-code bits (xterm 1006): low 2 bits select the button,
# plus flags for wheel, motion, and keyboard modifiers.
_SGR_BUTTON = 0x03
_SGR_SHIFT = 0x04
_SGR_ALT = 0x08
_SGR_CTRL = 0x10
_SGR_MOTION = 0x20
_SGR_WHEEL = 0x40
#: Buttons 8–11 (xterm): 128 + n-8. 8 and 9 are the side buttons.
_SGR_EXTRA = 0x80


def _escape_complete(buf: str) -> bool:
    """True once ``buf`` (the bytes after an ESC) forms a complete escape
    sequence: a CSI (``[`` … a final byte 0x40-0x7E), an SS3 (``O`` + one char),
    or a single meta char. An empty buffer is a bare ESC and never completes."""
    if not buf:
        return False
    if buf[0] == "[":
        return len(buf) >= 2 and "\x40" <= buf[-1] <= "\x7e"
    if buf[0] == "O":
        return len(buf) >= 2
    return True  # ESC + a single (meta) char


def _csi_modifiers(param: int) -> frozenset[str]:
    """Decode an xterm key-modifier parameter (``1 + bitmask`` of Shift=1,
    Alt=2, Ctrl=4, Meta=8) into contract modifier names. ``1`` (or ``0``, an
    absent parameter) means no modifier."""
    bits = param - 1 if param > 0 else 0
    names = []
    if bits & 1:
        names.append("shift")
    if bits & 2:
        names.append("alt")
    if bits & 4:
        names.append("ctrl")
    if bits & 8:
        names.append("cmd")
    return frozenset(names)


def _parse_csi_key(seq: str) -> "Event | None":
    """Decode a CSI/SS3 function-key sequence — ESC already stripped, e.g.
    ``[1;5D`` (Ctrl+Left), ``[3;3~`` (Alt+Delete), ``OC`` (Right) — into a
    modified KEY event, or None when it is not a key we recognize."""
    body = seq[1:]
    if not body:
        return None
    if seq[0] == "[" and body[-1] == "u":
        return _parse_csi_u(body[:-1])
    final = body[-1]
    params = body[:-1].split(";") if body[:-1] else []
    mod = int(params[1]) if len(params) >= 2 and params[1].isdigit() else 1
    modifiers = _csi_modifiers(mod)
    if final == "Z":
        # Shift+Tab has its own final byte with the shift implied, not encoded
        # in the modifier parameter; deliver it as shift+tab so focus traversal
        # goes backward, matching ncurses' KEY_BTAB path.
        return Event(type=EventType.KEY, key="tab",
                     modifiers=modifiers | frozenset({"shift"}))
    name = _CSI_FINAL_KEYS.get(final)
    if name is not None:
        return Event(type=EventType.KEY, key=name, modifiers=modifiers)
    if final == "~" and params and params[0].isdigit():
        name = _CSI_TILDE_KEYS.get(params[0])
        if name is not None:
            return Event(type=EventType.KEY, key=name, modifiers=modifiers)
    return None


#: The keys a ``CSI <code> u`` names by a control code rather than a glyph.
_CSI_U_KEYS = {9: "tab", 13: "enter", 27: "escape", 127: "backspace", 8: "backspace"}


def _parse_csi_u(params: str) -> "Event | None":
    """A ``CSI <code>[:alternates] [; <modifiers>[:event]] u`` key - the form
    (fixterms / the kitty keyboard protocol) a terminal such as Ghostty,
    kitty or WezTerm sends for a chord the legacy bytes cannot carry:
    Ctrl+` is ``[96;5u``. ``code`` is the key's Unicode code point. A
    printable comes out as it does from a GUI backend (Ctrl+` is key "`"
    with ctrl); None for what is not a key press (a release report)."""
    fields = params.split(";")
    code_s = fields[0].split(":")[0]
    if not code_s.isdigit():
        return None
    code = int(code_s)
    mod, event_type = 1, 1
    if len(fields) >= 2 and fields[1]:
        parts = fields[1].split(":")
        if parts[0].isdigit():
            mod = int(parts[0])
        if len(parts) >= 2 and parts[1].isdigit():
            event_type = int(parts[1])
    if event_type == 3:
        return None  # a key release
    modifiers = _csi_modifiers(mod)
    name = _CSI_U_KEYS.get(code)
    if name is not None:
        return Event(type=EventType.KEY, key=name, modifiers=modifiers)
    if code < 32 or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
        return None
    char = chr(code)
    if char.isalpha() and "shift" in modifiers:
        char = char.upper()
    if not is_typed_char(char):
        return None
    return char_key_event(char, modifiers)


def _meta_char_event(ch: str) -> "Event | None":
    """An ESC-prefixed single char is an Alt/Meta chord (readline word editing):
    Alt+Backspace (ESC DEL/BS) deletes the previous word, Alt+b / Alt+f move by
    word, Alt+d deletes the next word. Other Alt chords are left unhandled."""
    if ch in ("\x7f", "\x08"):
        return Event(type=EventType.KEY, key="backspace", modifiers=frozenset({"alt"}))
    key = _META_WORD_KEYS.get(ch.lower())
    if key is not None:
        return Event(type=EventType.KEY, key=key, modifiers=frozenset({"alt"}))
    return None


# --- the stream parser (POSIX console) ----------------------------------------


def parse_vt_input(text: str, flush: bool = False) -> tuple[list[dict], str]:
    """Parse a decoded VT input stream into backend input records.

    Returns the records and the unconsumed tail — an escape sequence whose
    remaining bytes have not arrived yet. ``flush=True`` forces a decision on
    that tail (the caller's grace wait for a continuation has expired): the
    leading ESC becomes the Escape key, which is what a lone ESC that never
    grew into a sequence was.

    A recognized sequence becomes a key or mouse record; an *unrecognized* CSI
    is dropped, never delivered as typing — that is where a terminal's replies
    to our own questions (``CSI 14 t``, ``CSI 18 t``) arrive, and the VT
    backend has already been bitten once by a reply being typed into the app.
    """
    records: list[dict] = []
    i = 0
    end_of_text = len(text)
    while i < end_of_text:
        ch = text[i]
        if ch != "\x1b":
            records.append(_char_record(ch))
            i += 1
            continue
        if i + 1 < end_of_text and text[i + 1] == "\x1b":
            # ESC ESC: the first was the Escape key and the second opens what
            # follows. Taken as an Alt chord instead, it swallowed the second
            # ESC, and an SGR mouse report right behind an Escape press was
            # typed into the app — ``;`` and all (xefm#656).
            records.append(_escape_key_record())
            i += 1
            continue
        end = _escape_end(text, i + 1)
        if end is None:
            if not flush:
                return records, text[i:]
            # The ESC was a keypress; drop the partial body that followed it
            # (it is always the suffix of the text), matching the curses
            # backend's fallback for a sequence that never completed.
            records.append(_escape_key_record())
            break
        records.extend(_escape_records(text[i + 1:end]))
        i = end
    return records, ""


#: What is left of a CSI/SS3 cut after its introducer: parameter bytes,
#: intermediates, a final byte.
_CUT_BODY_TAIL = re.compile(r"[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
#: A whole sequence body arriving after its ESC was given up on. SS3 only with
#: the finals keys send (arrows, Home/End, F1-F4), so typed "Ok" is not one.
_CUT_ESC_TAIL = re.compile(r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]|O[A-DFHP-S]")


def strip_late_tail(text: str, cut: str) -> str:
    """``text`` without the leading rest of a sequence the grace wait gave up on.

    Past the grace wait, ``parse_vt_input(flush=True)`` decides the ESC was a
    keypress and drops whatever body had come with it. On a slow link the rest
    arrives afterwards and, with no ESC in front, was typed: a cut ``ESC [1;5``
    left a ``D``, a cut mouse report its ``;12M`` — each one a command key in an
    application that binds letters.

    ``cut`` says what was cut. ``"body"``: an ESC and part of a body, so what
    follows can only be the rest of that body, and is dropped when it has that
    shape. ``"esc"``: a lone ESC, which is also what the Escape key sends; only
    a whole body in one read (``[A``, ``OP``, ``[<0;5;7M``) is dropped, since a
    person typing ``[`` sends it in a read of its own.
    """
    pattern = _CUT_BODY_TAIL if cut == "body" else _CUT_ESC_TAIL
    match = pattern.match(text)
    return text[match.end():] if match else text


def _escape_end(text: str, start: int) -> "int | None":
    """Index just past the escape sequence whose payload begins at ``start``
    (ESC already consumed), or None while it is still incomplete. The same
    grammar as :func:`_escape_complete`, scanned without re-slicing per byte."""
    if start >= len(text):
        return None
    lead = text[start]
    if lead == "[":
        for j in range(start + 1, len(text)):
            if "\x40" <= text[j] <= "\x7e":
                return j + 1
        return None
    if lead == "O":
        return start + 2 if start + 1 < len(text) else None
    return start + 1  # ESC + a single (meta) char


def _escape_records(seq: str) -> list[dict]:
    """Records for one complete escape payload (ESC stripped)."""
    if seq.startswith("[<") and seq[-1] in ("M", "m"):
        return _sgr_mouse_records(seq)
    if len(seq) == 1 and seq[0] not in "[O":
        event = _meta_char_event(seq)
        # An unhandled Alt chord falls back to Escape, matching the curses
        # backend, so the two TUI backends agree on every chord.
        return [_event_record(event)] if event is not None else [_escape_key_record()]
    event = _parse_csi_key(seq)
    if event is None:
        return []  # an unrecognized CSI: a terminal's reply, not typing
    return [_event_record(event)]


def _sgr_mouse_records(seq: str) -> list[dict]:
    """Decode one SGR mouse report ``[<b;x;yM`` (press / motion) or ``…m``
    (release) into gesture records.

    SGR names the button in the release report too — that is what lets this be
    stateless: down / up / drag / wheel come straight off the wire, the same
    gestures the Windows console derives by diffing button state.
    """
    try:
        b, x, y = (int(p) for p in seq[2:-1].split(";"))
    except ValueError:
        return []
    x, y = x - 1, y - 1  # 1-based in the protocol
    mods = set()
    if b & _SGR_SHIFT:
        mods.add("shift")
    if b & _SGR_CTRL:
        mods.add("ctrl")
    if b & _SGR_ALT:
        mods.add("alt")
    mods = frozenset(mods)
    low = b & _SGR_BUTTON
    if b & _SGR_EXTRA:
        # A side button: an atomic click on press, as the GUI backends report
        # it; its release and any motion with it are dropped.
        button = {0: "back", 1: "forward"}.get(low)
        if button is None or seq[-1] == "m" or b & _SGR_MOTION:
            return []
        return [{"type": "mouse", "action": "click", "x": x, "y": y,
                 "button": button, "mods": mods}]
    if b & _SGR_WHEEL:
        # 64 up, 65 down, 66 wheel-left, 67 wheel-right. Away from the user —
        # and rightward, matching Windows' positive HWHEEL — is positive.
        axis = "h" if low in (2, 3) else "v"
        step = 1 if low in (0, 3) else -1
        return [{"type": "mouse", "action": "wheel", "x": x, "y": y,
                 "wheel": step, "axis": axis, "mods": mods}]
    button = {0: "left", 1: "middle", 2: "right"}.get(low)
    if b & _SGR_MOTION:
        if button is None:
            # Bare motion: all-motion tracking (mode 1003), the POSIX
            # console's default (hover; see _vt_posix.HOVER).
            return [{"type": "mouse", "action": "move", "x": x, "y": y,
                     "mods": mods}]
        return [{"type": "mouse", "action": "drag", "x": x, "y": y,
                 "button": button, "mods": mods}]
    if button is None:
        return []
    action = "up" if seq[-1] == "m" else "down"
    return [{"type": "mouse", "action": action, "x": x, "y": y,
             "button": button, "mods": mods}]


def _char_record(ch: str) -> dict:
    # A terminal cannot report Shift for a printable; an uppercase letter
    # implies it, and the engine's shared contract helper lowercases the key.
    mods = frozenset({"shift"}) if (ch.isalpha() and ch.isupper()) else frozenset()
    return {"type": "key", "char": ch, "name": None, "mods": mods}


def _escape_key_record() -> dict:
    return {"type": "key", "char": "\x1b", "name": "escape", "mods": frozenset()}


def _event_record(event: Event) -> dict:
    return {"type": "key", "char": event.char or "", "name": event.key,
            "mods": event.modifiers or frozenset()}
