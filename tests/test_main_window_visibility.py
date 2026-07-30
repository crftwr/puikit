"""start_hidden / hide_main_window / is_main_window_visible (additive API).

The GUI backends implement these against a real window (ShowWindow/orderOut);
here the base-class contract and the MemoryBackend stand-in are pinned so
tray-app show/hide logic is testable headlessly.
"""

from puikit.backend import Backend
from puikit.backends.memory_backend import MemoryBackend


class TestBaseContract:

    def test_base_defaults(self):
        # A backend that cannot hide its surface: hide is a no-op, visibility
        # reports True — safe to call unconditionally from apps.
        assert Backend.is_main_window_visible(MemoryBackend.__new__(MemoryBackend))
        Backend.hide_main_window(MemoryBackend.__new__(MemoryBackend))
        Backend.show_main_window(MemoryBackend.__new__(MemoryBackend))


class TestMemoryBackendVisibility:

    def test_open_shows_by_default(self):
        backend = MemoryBackend()
        assert not backend.is_main_window_visible()  # not open yet
        backend.open()
        assert backend.is_main_window_visible()

    def test_start_hidden(self):
        backend = MemoryBackend(start_hidden=True)
        backend.open()
        assert not backend.is_main_window_visible()
        backend.show_main_window()
        assert backend.is_main_window_visible()

    def test_hide_and_reshow(self):
        backend = MemoryBackend()
        backend.open()
        backend.hide_main_window()
        assert not backend.is_main_window_visible()
        backend.show_main_window()
        assert backend.is_main_window_visible()

    def test_old_positional_ctor_still_binds(self):
        # Compat checklist: start_hidden was appended after activation_policy.
        backend = MemoryBackend(40, 12, None, None, "accessory")
        assert backend.size == (40, 12)
        assert backend.activation_policy == "accessory"
        assert backend._start_hidden is False
