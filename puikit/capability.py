"""Capability profiles for PuiKit backends.

Backends declare what they can do via a CapabilityProfile. The Panel layer
interprets the profile and resolves fallbacks; apps and widgets never look
at capabilities directly.
"""

from __future__ import annotations


class CapabilityProfile(dict):
    """A capability table. Unknown capabilities are reported as unsupported."""

    def __missing__(self, key: str) -> bool:
        return False

    def supports(self, name: str) -> bool:
        return bool(self.get(name, False))


PROFILE_TUI = CapabilityProfile(
    pixel_layout=False,
    hairline=False,  # sub-unit divider lines (zero base unit cost)
    vector_shapes=False,     # rounded rects / ellipses / vector marks (a modern,
                             # non-character look for controls); grid backends fall
                             # back to box-drawing + ASCII marks in the Panel layer
    fonts=False,             # real faces, sizes, weights, slants
    proportional_text=False,  # variable-advance text (no character grid)
    layering=False,
    transparency=False,
    shadow=False,
    animation=False,
    drag_and_drop=False,     # drop-IN: accept files/text dropped onto the app
    os_drag_drop=False,      # drag-OUT: be an OS drag *source* (e.g. drag a file
                             # to Finder). Needs a native window/view, so a
                             # terminal app can never be one; the Panel falls
                             # back to copying paths to the clipboard.
    os_open=False,           # no OS shell to open a URL; Panel copies it instead
    ime=False,
    clipboard_rich=False,
    native_file_dialog=False,
    system_tray=False,
    native_menus=False,      # OS menu bar / context menus (NSMenu, HMENU, ...);
                             # the Panel falls back to a widget-rendered menu
    hover=False,
    media_keys=False,
    icons=False,
    images=False,
)

PROFILE_GUI_WEB = CapabilityProfile(
    pixel_layout=True,
    hairline=True,
    vector_shapes=True,
    fonts=True,
    proportional_text=True,
    layering=True,
    transparency=True,
    shadow=True,
    animation=True,
    drag_and_drop=True,    # drop-IN, browser-limited
    os_drag_drop=False,    # drag-OUT: no OS file-drag source from the browser
    os_open=True,          # open a clicked URL/file in the OS handler
    ime=True,
    clipboard_rich=False,  # security-limited
    native_file_dialog=False,
    system_tray=False,
    native_menus=False,    # no OS-level app menu bar in the browser
    hover=True,
    media_keys=False,
    icons=True,
    images=True,
)

PROFILE_GUI_DESKTOP = CapabilityProfile(
    {
        **PROFILE_GUI_WEB,
        "clipboard_rich": True,
        "native_file_dialog": True,
        "system_tray": True,
        "native_menus": True,
        "os_drag_drop": True,  # native NSDraggingSource: drag files to other apps
        "gpu_acceleration": True,
        "media_keys": True,
    }
)

PROFILE_MOBILE = CapabilityProfile(
    {
        **PROFILE_GUI_WEB,
        "system_tray": False,
        "media_keys": False,
        "native_file_dialog": False,
        "touch": True,
        "virtual_keyboard": True,
        "gpu_acceleration": True,
    }
)

PROFILE_GAME = CapabilityProfile(
    pixel_layout=True,
    hairline=True,
    vector_shapes=True,
    fonts=True,
    proportional_text=True,
    layering=True,
    transparency=True,
    shadow=False,          # app-rendered if needed
    animation=True,
    drag_and_drop=False,
    ime=False,
    clipboard_rich=False,
    native_file_dialog=False,
    system_tray=False,
    hover=True,
    media_keys=False,
    touch=True,            # platform-dependent
    gamepad=True,
    gpu_acceleration=True,
    icons=True,
    images=True,
)
