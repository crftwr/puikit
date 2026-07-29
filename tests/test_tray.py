"""set_tray contract tests that run on any OS: the base signature, the base
capability error, and the memory backend's recording. The GUI backends' tray
behavior is tested in their platform-gated modules (test_macos_backend.py /
test_windows_backend.py)."""

import inspect

import pytest

from puikit.backend import Backend, CapabilityNotSupported
from puikit.backends.memory_backend import MemoryBackend


def test_base_signature_is_additive():
    # Pre-image callers pass (title, menu, tooltip) positionally; image must
    # extend the end with a None default, per the additive API policy.
    sig = inspect.signature(Backend.set_tray)
    assert list(sig.parameters) == ["self", "title", "menu", "tooltip", "image"]
    assert all(sig.parameters[name].default is None
               for name in ("title", "menu", "tooltip", "image"))


def test_base_raises_capability_not_supported():
    with pytest.raises(CapabilityNotSupported):
        Backend.set_tray(None, "K")


def test_memory_backend_records_image():
    backend = MemoryBackend(width=20, height=5)
    backend.set_tray("K", None, tooltip="Keyhac", image="/x/keyhac.ico")
    backend.set_tray(image="/x/MenuExtraTemplate.png")
    backend.set_tray()  # remove
    assert backend.tray_calls == [
        ("K", None, "Keyhac", "/x/keyhac.ico"),
        (None, None, None, "/x/MenuExtraTemplate.png"),
        (None, None, None, None),
    ]
