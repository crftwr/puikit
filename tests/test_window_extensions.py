"""WindowStyle, activation_policy, and call_later (additive API extensions)."""

import dataclasses
import sys
import threading

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


def _run_in_thread(fn):
    """Run fn on a worker thread; return {'value': ...} or {'error': exc}."""
    result = {}

    def target():
        try:
            result["value"] = fn()
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join()
    return result


class TestUiThreadGuard:
    """call_later is UI-thread-only and enforced identically on every backend.

    Without the guard the failure diverged per platform: an NSTimer scheduled
    from a worker thread attaches to that thread's non-running run loop and
    silently never fires, while the same mistake happened to work on Windows
    and the tick-fallback backends."""

    def test_worker_thread_call_later_raises_after_open(self):
        backend = MemoryBackend()
        backend.open()  # arms the guard: this thread is the UI thread
        result = _run_in_thread(lambda: backend.call_later(0.1, lambda: None))
        assert isinstance(result.get("error"), RuntimeError)
        assert "call_on_main_thread" in str(result["error"])
        assert backend.later_timers == []      # nothing was scheduled

    def test_worker_thread_cancel_raises_after_open(self):
        backend = MemoryBackend()
        backend.open()
        cancel = backend.call_later(0.1, lambda: None)
        result = _run_in_thread(cancel)
        assert isinstance(result.get("error"), RuntimeError)
        assert backend.fire_timers() == 1      # the failed cancel cancelled nothing

    def test_ui_thread_still_works_after_open(self):
        backend = MemoryBackend()
        backend.open()
        fired = []
        backend.call_later(0.1, lambda: fired.append(1))
        assert backend.fire_timers() == 1
        assert fired == [1]

    def test_guard_inert_before_open(self):
        # Headless construction without open() (common in tests) stays
        # unrestricted: the guard only arms once open() declares the UI thread.
        backend = MemoryBackend()
        result = _run_in_thread(lambda: backend.call_later(0.1, lambda: None))
        assert "error" not in result

    def test_base_fallback_guarded_too(self):
        # The Backend base implementation (animation-tick fallback) enforces
        # the same contract.
        class TickBackend(MemoryBackend):
            def call_later(self, delay_seconds, callback):
                from puikit.backend import Backend
                return Backend.call_later(self, delay_seconds, callback)

            def request_animation_ticks(self, callback):
                self.tick_callbacks.append(callback)

        backend = TickBackend()
        backend.open()
        result = _run_in_thread(lambda: backend.call_later(0.1, lambda: None))
        assert isinstance(result.get("error"), RuntimeError)
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
