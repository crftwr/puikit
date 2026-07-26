"""WindowStyle, activation_policy, and call_later (additive API extensions)."""

import dataclasses
import sys

import pytest

from puikit import PROFILE_GUI_DESKTOP, WindowStyle
from puikit.backends.memory_backend import MemoryBackend


class TestWindowStyleDataclass:

    def test_defaults_reproduce_classic_window(self):
        ws = WindowStyle()
        assert ws.frameless is False
        assert ws.topmost is False
        assert ws.activates is True
        assert ws.resizable is True
        assert ws.tool is False

    def test_positional_arity_binds(self):
        # Compat checklist: old positional construction must keep binding as
        # fields are appended.
        ws = WindowStyle(True, True, False, False, True)
        assert (ws.frameless, ws.topmost, ws.activates, ws.resizable, ws.tool) == (
            True, True, False, False, True)

    def test_round_trips(self):
        old = WindowStyle(frameless=True, topmost=True)
        assert WindowStyle(**dataclasses.asdict(old)) == old
        assert dataclasses.replace(old, topmost=False) == WindowStyle(frameless=True)

    def test_capability_declared(self):
        assert PROFILE_GUI_DESKTOP.supports("window_styles")


class TestMemoryBackendParity:

    def test_old_positional_ctor_still_binds(self):
        backend = MemoryBackend(40, 12, None)
        assert backend.size == (40, 12)
        assert backend.window_style == WindowStyle()
        assert backend.activation_policy == "regular"

    def test_style_and_policy_recorded(self):
        ws = WindowStyle(frameless=True, topmost=True, activates=False)
        backend = MemoryBackend(style=ws, activation_policy="accessory")
        assert backend.window_style is ws
        assert backend.activation_policy == "accessory"


class TestCallLaterMemory:

    def test_fires_in_order(self):
        backend = MemoryBackend()
        fired = []
        backend.call_later(0.1, lambda: fired.append("a"))
        backend.call_later(0.2, lambda: fired.append("b"))
        assert fired == []
        assert backend.fire_timers() == 2
        assert fired == ["a", "b"]
        assert backend.later_timers == []

    def test_cancel(self):
        backend = MemoryBackend()
        fired = []
        cancel = backend.call_later(0.1, lambda: fired.append("a"))
        cancel()
        cancel()  # cancelling twice is a no-op
        assert backend.fire_timers() == 0
        assert fired == []


class TestCallLaterBaseFallback:
    """The Backend base implementation rides request_animation_ticks."""

    class TickBackend(MemoryBackend):
        # Bypass MemoryBackend's recording override to exercise the base.
        def call_later(self, delay_seconds, callback):
            from puikit.backend import Backend
            return Backend.call_later(self, delay_seconds, callback)

        def request_animation_ticks(self, callback):
            self.tick_callbacks.append(callback)

        def tick(self):
            self.tick_callbacks = [cb for cb in self.tick_callbacks if cb()]

    def test_fires_after_deadline(self, monkeypatch):
        import puikit.backend as backend_mod
        now = {"t": 100.0}
        monkeypatch.setattr(backend_mod.time, "monotonic", lambda: now["t"])

        backend = self.TickBackend()
        fired = []
        backend.call_later(1.0, lambda: fired.append(1))

        backend.tick()
        assert fired == []                      # not due yet
        now["t"] = 100.5
        backend.tick()
        assert fired == []
        now["t"] = 101.0
        backend.tick()
        assert fired == [1]                     # due exactly at the deadline
        assert backend.tick_callbacks == []     # unregistered after firing

    def test_cancel_unregisters(self, monkeypatch):
        import puikit.backend as backend_mod
        now = {"t": 0.0}
        monkeypatch.setattr(backend_mod.time, "monotonic", lambda: now["t"])

        backend = self.TickBackend()
        fired = []
        cancel = backend.call_later(1.0, lambda: fired.append(1))
        cancel()
        now["t"] = 2.0
        backend.tick()
        assert fired == []
        assert backend.tick_callbacks == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS backend")
class TestMacOSBackendSignature:

    def test_ctor_accepts_new_kwargs_without_open(self):
        from puikit.backends.macos_backend import MacOSBackend
        backend = MacOSBackend(
            80, 24, "t",
            style=WindowStyle(frameless=True, topmost=True, activates=False),
            activation_policy="accessory",
        )
        assert backend._window_style.frameless is True
        assert backend._activation_policy == "accessory"

    def test_old_positional_ctor_still_binds(self):
        from puikit.backends.macos_backend import MacOSBackend
        backend = MacOSBackend(80, 24, "title", None, None, "frame-name")
        assert backend._frame_autosave_name == "frame-name"
        assert backend._window_style == WindowStyle()
        assert backend._activation_policy == "regular"
