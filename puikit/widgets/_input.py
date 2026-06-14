"""Small input helpers shared by the interactive widgets.

Keeps key/character interpretation in one place so every control agrees on
what "activate" and "a printable character" mean across backends.
"""

from __future__ import annotations

from ..event import Event, EventType


def is_activate(event: Event) -> bool:
    """True for the keys that activate a control: enter or space. Space arrives
    as a printable char on some backends and a symbolic name on others, so
    accept both spellings."""
    return event.type is EventType.KEY and (
        event.key in ("enter", "space") or event.char == " "
    )


def typed_char(event: Event) -> str | None:
    """The single printable character a KEY event carries, or None. Symbolic
    keys (arrows, enter, tab, backspace) report no char, so they are skipped —
    only real text insertion returns a value."""
    if event.type is not EventType.KEY:
        return None
    ch = event.char
    if ch and len(ch) == 1 and ch.isprintable():
        return ch
    return None
