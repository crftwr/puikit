"""Pure-logic tests for the Windows menu plumbing (_win32_menu.py): the
responder's bookkeeping and the Panel's modality gate, with user32 stubbed so
no real HMENU is needed. The macOS half of the same contract is covered in
test_macos_backend.py (the native-menu section)."""

import sys

import pytest

# Skip at COLLECTION time: the import below reaches _win32_native, whose module
# body calls ctypes.WinDLL, which does not exist off Windows.
if sys.platform != "win32":
    pytest.skip("Windows-only backend", allow_module_level=True)

from puikit.backends import _win32_menu as wm  # noqa: E402
from puikit.menu import Menu, MenuItem  # noqa: E402

_HMENU = 0x1234  # a stand-in handle; nothing here calls into the real menu API


class _FakeUser32:
    """Records the EnableMenuItem/CheckMenuItem calls revalidate makes."""

    def __init__(self):
        self.enabled = {}
        self.checked = {}

    def EnableMenuItem(self, hmenu, tag, flags):
        self.enabled[tag] = flags

    def CheckMenuItem(self, hmenu, tag, flags):
        self.checked[tag] = flags


@pytest.fixture
def user32(monkeypatch):
    fake = _FakeUser32()
    monkeypatch.setattr(wm.w, "user32", fake)
    return fake


def _greyed(user32, tag):
    return bool(user32.enabled[tag] & wm.w.MF_GRAYED)


def test_menu_bar_greys_out_whole_while_the_gate_says_inert(user32):
    # xefm#388: the OS bar is outside the Panel's layer stack, so a click on it
    # never passes through dispatch_event. While a modal layer owns the app,
    # every item greys out, whatever its own enabled predicate says.
    active = {"value": True}
    item = MenuItem("New", on_select=lambda: None)
    responder = wm.MenuResponder()
    tag = responder.register(item, lambda: active["value"])
    responder.register_popup(_HMENU, Menu(item), lambda: active["value"])

    responder.revalidate(_HMENU)
    assert not _greyed(user32, tag)

    active["value"] = False
    responder.revalidate(_HMENU)
    assert _greyed(user32, tag)


def test_inert_menu_bar_item_refuses_to_fire():
    # Win32 sends no WM_COMMAND for a greyed item, so this is the second lock
    # on a door the OS has already shut.
    active = {"value": False}
    fired = []
    item = MenuItem("New", on_select=lambda: fired.append(True))
    responder = wm.MenuResponder()
    tag = responder.register(item, lambda: active["value"])

    responder.fire(tag)
    assert fired == []

    active["value"] = True
    responder.fire(tag)
    assert fired == [True]


def test_popup_and_tray_menus_are_never_gated(user32):
    # One responder serves the bar, every context popup and the tray menu. Only
    # the bar carries a gate: a popup is raised by whatever surface is already
    # on top, and the tray icon is outside the window altogether.
    fired = []
    item = MenuItem("Open", on_select=lambda: fired.append(True))
    responder = wm.MenuResponder()
    tag = responder.register(item)
    responder.register_popup(_HMENU, Menu(item))

    responder.revalidate(_HMENU)
    assert not _greyed(user32, tag)
    responder.fire(tag)
    assert fired == [True]


def test_disabled_item_stays_disabled_under_an_active_gate(user32):
    # The gate is an extra condition, not a replacement for the item's own.
    item = MenuItem("Paste", enabled=False)
    responder = wm.MenuResponder()
    tag = responder.register(item, lambda: True)
    responder.register_popup(_HMENU, Menu(item), lambda: True)

    responder.revalidate(_HMENU)
    assert _greyed(user32, tag)
