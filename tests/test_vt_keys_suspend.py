"""Key naming and suspend/resume on the VT backend.

Both are things the spike got wrong in ways only real use exposed: space was
delivered as the literal " " rather than the contract's named ``"space"``, so an
app's binding never matched; and ``suspended()`` was never overridden at all, so
the base no-op left the alternate screen up and the console raw while an editor
ran, and returning showed the wreckage of both.
"""

import io

import pytest

from puikit.backends.vt_backend import VTBackend, _StreamConsole
from puikit.event import EventType


class FakeConsole(_StreamConsole):
    def __init__(self, width=40, height=10):
        super().__init__(stream=io.StringIO(), size=(width, height))
        self._fixed = (width, height)
        self.marks: list[str] = []

    def size(self):
        return self._fixed

    def read_input(self, timeout_ms):
        return []

    def suspend(self):
        self.marks.append("suspend")
        super().suspend()

    def resume(self):
        self.marks.append("resume")
        super().resume()


@pytest.fixture
def backend():
    be = VTBackend(console=FakeConsole())
    be.open()
    yield be, be._console
    be.close()


def key(be, char="", vk=0, control=0):
    return be._to_event({"type": "key", "char": char, "vk": vk, "control": control})


# --- the keyboard contract -------------------------------------------------


def test_space_is_the_named_key(backend):
    # The bug: delivered as key=" ", so an app binding "space" never fired.
    be, _ = backend
    event = key(be, " ", 0x20)
    assert event.key == "space"
    assert event.char == " "   # kept, so text fields still insert it


def test_space_with_no_char_still_names_itself(backend):
    be, _ = backend
    assert key(be, "", 0x20).key == "space"


def test_shifted_letter_lowercases_and_keeps_shift(backend):
    be, _ = backend
    event = key(be, "A", 0x41, 0x0010)
    assert (event.key, event.char) == ("a", "A")
    assert event.modifiers == frozenset({"shift"})


def test_shifted_symbol_drops_the_redundant_shift(backend):
    # The produced glyph IS the identity, and shift is already baked into it —
    # so Shift+1 is ("!", {}) on every backend, never ("!", {"shift"}) on one.
    be, _ = backend
    event = key(be, "!", 0x31, 0x0010)
    assert event.key == "!"
    assert event.modifiers == frozenset()


@pytest.mark.parametrize("char,name", [
    ("\r", "enter"), ("\n", "enter"), ("\t", "tab"),
    ("\x1b", "escape"), ("\x7f", "backspace"), ("\x08", "backspace"),
])
def test_control_characters_map_to_named_keys(backend, char, name):
    be, _ = backend
    assert key(be, char, 0).key == name


def test_ctrl_letter_arrives_as_a_modified_letter(backend):
    # Ctrl+A/C/X/V must work here exactly as under curses.
    be, _ = backend
    event = key(be, "\x01", 0x41, 0x0008)
    assert event.key == "a"
    assert "ctrl" in event.modifiers


def test_ime_commit_is_still_not_dropped(backend):
    # The routing change must not regress the reason this backend exists.
    be, _ = backend
    assert key(be, "あ", 0xE5).key == "あ"


# --- suspend / resume ------------------------------------------------------


def test_suspend_releases_and_resume_reclaims_the_terminal(backend):
    be, con = backend
    with be.suspended():
        released = "".join(con.written)
        # The child needs the normal screen and a visible cursor.
        assert "\x1b[?1049l" in released
        assert "\x1b[?25h" in released
    assert con.marks == ["suspend", "resume"]
    assert "\x1b[?1049h" in "".join(con.written)   # back on the alternate screen


def test_resume_repaints_everything(backend):
    # The alternate screen comes back BLANK while the diff still believes every
    # cell it last sent is on display — so without a forced repaint the screen
    # stays empty (or shows the child's leavings).
    be, con = backend
    be.clear()
    be.draw_text(0, 0, "important")
    be.present()
    con.written.clear()
    with be.suspended():
        pass
    assert "important" in "".join(con.written)


def test_resume_retransmits_images(backend, tmp_path):
    # The child wiped the pixels off the screen as surely as it wiped the text,
    # and an unchanged placement would otherwise never be re-sent: the diff
    # skips it precisely because it did not change.
    pytest.importorskip("PIL")
    from PIL import Image

    png = tmp_path / "pic.png"
    Image.new("RGB", (8, 8), (10, 200, 10)).save(png)

    be, con = backend
    be._term_graphics = "sixel"
    placement = (0, 0, 2, 2, str(png), None)
    # Two frames, so the placement is genuinely "unchanged" by the second.
    for _ in range(2):
        be.clear()
        be._images = {1: placement}
        be.present()
    con.written.clear()
    be.clear()
    be._images = {1: placement}
    be.present()
    assert "\x1b7" not in "".join(con.written)   # steady state: not re-sent

    con.written.clear()
    with be.suspended():
        pass
    assert "\x1b7" in "".join(con.written)       # after a suspend: sent again


def test_suspend_is_safe_before_open():
    be = VTBackend(console=FakeConsole())
    with be.suspended():
        pass  # no grid yet; must not raise


# --- animation ticks -------------------------------------------------------


def test_repeated_registration_does_not_accumulate(backend):
    # The Panel registers the SAME bound method from half a dozen places every
    # time an animation starts. Appending blindly meant the list grew for the
    # life of the session and every entry fired on every idle wake, so an
    # animated theme got slower the longer it ran, with no single action to
    # blame — which is exactly how it was reported.
    be, _ = backend
    calls = []

    def tick():
        calls.append(1)
        return True

    for _ in range(50):
        be.request_animation_ticks(tick)
    assert len(be._tick_callbacks) == 1

    be._run_ticks()
    assert len(calls) == 1   # once per wake, not fifty times


def test_a_finished_tick_unregisters_itself(backend):
    # Returning False is the callback's way of saying it is done; ignoring it
    # kept every animation that had ever run registered forever.
    be, _ = backend
    be.request_animation_ticks(lambda: False)
    be._run_ticks()
    assert be._tick_callbacks == []


def test_a_live_tick_stays_registered(backend):
    be, _ = backend
    be.request_animation_ticks(lambda: True)
    for _ in range(3):
        be._run_ticks()
    assert len(be._tick_callbacks) == 1
