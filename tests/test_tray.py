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
    assert list(sig.parameters) == ["self", "title", "menu", "tooltip", "image",
                                    "badge"]
    assert all(sig.parameters[name].default is None
               for name in ("title", "menu", "tooltip", "image", "badge"))


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


def test_memory_backend_records_badge_apart_from_the_calls():
    """badge is appended last with a None default, and recorded in its own
    list: tray_calls keeps the 4-tuples existing callers unpack."""
    backend = MemoryBackend(width=20, height=5)
    backend.set_tray("K", None, "Keyhac", "/x/keyhac.ico")
    backend.set_tray(image="/x/keyhac.ico", badge=(230, 40, 40))
    assert backend.tray_calls == [("K", None, "Keyhac", "/x/keyhac.ico"),
                                  (None, None, None, "/x/keyhac.ico")]
    assert backend.tray_badges == [None, (230, 40, 40)]


def test_set_tray_image_is_a_base_no_op():
    """A new Backend method with a base no-op, per the additive policy: an
    animation driver can call it on any backend."""
    assert Backend.set_tray_image(None, "/x/frame.png") is None


def test_memory_backend_records_image_swaps():
    backend = MemoryBackend(width=20, height=5)
    backend.set_tray(image="/x/a.png")
    backend.set_tray_image("/x/b.png")
    backend.set_tray_image("/x/a.png")
    assert backend.tray_images == ["/x/b.png", "/x/a.png"]
    assert len(backend.tray_calls) == 1, "the item itself was not restated"


def test_main_window_active_defaults_to_visible():
    """An additive Backend method: a backend without window activation
    answers as it does for visibility."""
    backend = MemoryBackend(width=20, height=5)
    assert Backend.is_main_window_active(backend) == backend.is_main_window_visible()


def test_memory_backend_models_a_covered_window():
    backend = MemoryBackend(width=20, height=5)
    backend.open()
    assert backend.is_main_window_visible() and backend.is_main_window_active()
    backend.main_window_active = False          # shown, but behind another
    assert backend.is_main_window_visible()
    assert not backend.is_main_window_active()


def test_post_notification_is_a_base_no_op_reporting_nothing_was_posted():
    backend = MemoryBackend(width=20, height=5)
    assert backend.post_notification("Done", "All 12 items") is False
    assert backend.capabilities["system_notifications"] is False
    sig = inspect.signature(Backend.post_notification)
    assert list(sig.parameters) == ["self", "title", "text"]
    assert sig.parameters["text"].default == ""
