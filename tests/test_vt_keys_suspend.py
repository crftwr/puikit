"""Key naming and suspend/resume on the VT backend.

Both are things the spike got wrong in ways only real use exposed: space was
delivered as the literal " " rather than the contract's named ``"space"``, so an
app's binding never matched; and ``suspended()`` was never overridden at all, so
the base no-op left the alternate screen up and the console raw while an editor
ran, and returning showed the wreckage of both.
"""

import io

import pytest

from puikit.backends.vt_backend import VTBackend, _StreamConsole, _win_key_record
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
    # Through the Windows translation, as the real _read_records feeds it.
    return be._to_event(_win_key_record(char, vk, control))


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


# --- bare-Alt tap (Windows console menu activation) -------------------------
#
# The tracker is a module-level pure class precisely so this is testable off
# Windows: it is fed (vk, keydown, control) exactly as _read_records does.

_VK_ALT = 0x12
_ALT_DOWN_STATE = 0x0002  # LEFT_ALT_PRESSED
_CTRL_STATE = 0x0008      # LEFT_CTRL_PRESSED


def _tracker():
    from puikit.backends.vt_backend import _AltTapTracker

    return _AltTapTracker()


def test_alt_tap_fires_on_the_bare_release():
    t = _tracker()
    assert t.feed_key(_VK_ALT, True, _ALT_DOWN_STATE) is False  # down only arms
    assert t.feed_key(_VK_ALT, False, 0) is True                # up: the tap


def test_alt_tap_survives_autorepeat():
    t = _tracker()
    for _ in range(3):  # a held Alt repeats its own down
        assert t.feed_key(_VK_ALT, True, _ALT_DOWN_STATE) is False
    assert t.feed_key(_VK_ALT, False, 0) is True


def test_alt_chord_never_fires():
    # Alt+X on its way to X must not open the menu (the whole reason the tap
    # fires on the release, as Windows itself does).
    t = _tracker()
    t.feed_key(_VK_ALT, True, _ALT_DOWN_STATE)
    t.feed_key(0x58, True, _ALT_DOWN_STATE)   # X down while Alt held
    t.feed_key(0x58, False, _ALT_DOWN_STATE)
    assert t.feed_key(_VK_ALT, False, 0) is False


def test_altgr_never_arms():
    # On layouts where AltGr reports as Ctrl+Alt, arming would turn every
    # AltGr glyph into a menu activation.
    t = _tracker()
    t.feed_key(_VK_ALT, True, _ALT_DOWN_STATE | _CTRL_STATE)
    assert t.feed_key(_VK_ALT, False, 0) is False


def test_mouse_press_disarms_the_tap():
    t = _tracker()
    t.feed_key(_VK_ALT, True, _ALT_DOWN_STATE)
    t.disarm()  # what _read_records does on a click/wheel gesture
    assert t.feed_key(_VK_ALT, False, 0) is False


def test_alt_record_becomes_the_named_key(backend):
    # The synthesized record travels the same _to_event path as everything else.
    be, _ = backend
    event = be._to_event({"type": "key", "char": "", "name": "alt",
                          "mods": frozenset()})
    assert event.type is EventType.KEY
    assert event.key == "alt"


# --- Alt+letter accelerators (Windows console) -------------------------------


def test_alt_letter_with_suppressed_char_recovers_the_letter(backend):
    # The console may leave UnicodeChar empty for an Alt chord; the letter VK
    # still names the key, so Alt+F reaches the app as ("f", {"alt"}).
    be, _ = backend
    event = key(be, "", 0x46, _ALT_DOWN_STATE)  # VK 'F', no char
    assert event.key == "f"
    assert event.modifiers == frozenset({"alt"})


def test_alt_letter_with_produced_char_keeps_the_modifier(backend):
    # And when the console does produce the character, the char path already
    # carries alt — either way the app sees the same event.
    be, _ = backend
    event = key(be, "f", 0x46, _ALT_DOWN_STATE)
    assert event.key == "f"
    assert event.modifiers == frozenset({"alt"})


def test_ctrl_alt_with_suppressed_char_stays_dropped(backend):
    # AltGr reports Ctrl+Alt; a suppressed char there means the chord really
    # produced nothing, so no letter is fabricated.
    be, _ = backend
    assert key(be, "", 0x46, _ALT_DOWN_STATE | _CTRL_STATE) is None


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
    placement = (0, 0, 2, 2, str(png), None, (0, 0, 2, 2, None))
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


# --- terminal replies ------------------------------------------------------


def key_record(char, vk=0):
    return _win_key_record(char, vk, 0)


def test_a_late_size_reply_is_not_typed_into_the_app():
    # The bug this guards: the size probe asks the terminal how big its text
    # area is, and VS Code's terminal answers AFTER the probe has given up
    # waiting. The reply then arrives as ordinary key records and is typed into
    # whatever has focus — "[8;26;136t", whose leading characters press whatever
    # they happen to be bound to on the way past.
    from puikit.backends.vt_backend import _strip_csi_replies

    held = []
    reply = [key_record(c) for c in "\x1b[8;26;136t"]
    assert _strip_csi_replies(reply, held) == []
    assert held == []


def test_a_reply_split_across_reads_is_still_swallowed():
    from puikit.backends.vt_backend import _strip_csi_replies

    held = []
    assert _strip_csi_replies([key_record("\x1b"), key_record("[")], held) == []
    assert held  # still arriving
    assert _strip_csi_replies([key_record(c) for c in "8;26;136t"], held) == []
    assert held == []


def test_a_real_escape_keypress_is_not_eaten():
    # The filter must not cost the Escape key. A lone ESC is held only until the
    # next character decides it: "[" means a reply, anything else means the key.
    from puikit.backends.vt_backend import _strip_csi_replies

    held = []
    out = _strip_csi_replies([key_record("\x1b", 0x1B), key_record("a", 0x41)], held)
    assert [r["char"] for r in out] == ["\x1b", "a"]
    assert held == []


def test_ordinary_typing_is_untouched():
    from puikit.backends.vt_backend import _strip_csi_replies

    held = []
    out = _strip_csi_replies([key_record("f"), key_record("j")], held)
    assert [r["char"] for r in out] == ["f", "j"]


def test_a_resize_is_never_part_of_a_reply():
    from puikit.backends.vt_backend import _strip_csi_replies

    held = []
    assert _strip_csi_replies([{"type": "resize"}], held) == [{"type": "resize"}]
