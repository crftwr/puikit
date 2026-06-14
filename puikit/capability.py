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
    layering=False,
    transparency=False,
    shadow=False,
    animation=False,
    drag_and_drop=False,
    ime=False,
    clipboard_rich=False,
    native_file_dialog=False,
    system_tray=False,
    hover=False,
    media_keys=False,
    icons=False,
    images=False,
)

PROFILE_GUI_WEB = CapabilityProfile(
    pixel_layout=True,
    hairline=True,
    layering=True,
    transparency=True,
    shadow=True,
    animation=True,
    drag_and_drop=True,    # browser-limited
    ime=True,
    clipboard_rich=False,  # security-limited
    native_file_dialog=False,
    system_tray=False,
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
