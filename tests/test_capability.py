from puikit import (
    CapabilityProfile,
    PROFILE_GUI_DESKTOP,
    PROFILE_GUI_WEB,
    PROFILE_MOBILE,
    PROFILE_TUI,
)


def test_unknown_capability_defaults_to_false():
    profile = CapabilityProfile()
    assert profile["nonexistent_capability"] is False
    assert not profile.supports("nonexistent_capability")


def test_tui_profile_is_minimal():
    assert not PROFILE_TUI.supports("pixel_layout")
    assert not PROFILE_TUI.supports("animation")
    assert not PROFILE_TUI.supports("icons")


def test_desktop_inherits_and_overrides_web():
    assert not PROFILE_GUI_WEB.supports("clipboard_rich")
    assert PROFILE_GUI_DESKTOP.supports("clipboard_rich")
    assert PROFILE_GUI_DESKTOP.supports("pixel_layout")
    assert PROFILE_GUI_DESKTOP.supports("system_tray")


def test_mobile_overrides_web():
    assert PROFILE_MOBILE.supports("touch")
    assert PROFILE_MOBILE.supports("virtual_keyboard")
    assert not PROFILE_MOBILE.supports("native_file_dialog")


def test_main_thread_dispatch_only_on_native_desktop():
    # A native run loop on a UI thread can accept work from workers; a browser
    # (single-threaded JS) and a TUI poll loop cannot.
    assert PROFILE_GUI_DESKTOP.supports("main_thread_dispatch")
    assert not PROFILE_GUI_WEB.supports("main_thread_dispatch")
    assert not PROFILE_TUI.supports("main_thread_dispatch")
