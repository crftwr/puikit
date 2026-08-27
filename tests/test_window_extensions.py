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
        assert ws.overlay_input == "none"

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


class TestOverlayInput:
    """What input reaches a window while its application is not active - the
    axis `activates=False` leaves open. One field rather than a flag per
    mechanism: the values are a ladder (keyboard includes clicks, mouse
    includes display), so no combination of them is meaningless."""

    def test_display_only_by_default_so_existing_overlays_are_unchanged(self):
        assert WindowStyle(activates=False).overlay_input == "none"

    def test_only_meaningful_with_activates_false(self):
        # Not enforced by the dataclass - the backends ignore it on an
        # activating window - but stated here so the pairing is part of the
        # contract and not folklore.
        ws = WindowStyle(activates=True, overlay_input="keyboard")
        assert ws.activates is True

    def test_round_trips_with_the_other_fields(self):
        ws = WindowStyle(topmost=True, resizable=False, activates=False,
                         overlay_input="mouse")
        assert WindowStyle(**dataclasses.asdict(ws)) == ws

    def test_the_two_usable_shapes_are_distinct(self):
        """"keyboard" is key (an input method composes in it, and whatever
        was focused underneath is not); "mouse" lets clicks through while the
        target keeps its focus, caret and selection. Both verified against a
        real macOS session; the dataclass only has to carry them apart."""
        keyboard = WindowStyle(activates=False, overlay_input="keyboard")
        clicks = dataclasses.replace(keyboard, overlay_input="mouse")
        assert keyboard != clicks

    def test_backend_without_the_capability_accepts_and_ignores_it(self):
        # The base recipe: an unknown request degrades, it does not raise.
        ws = WindowStyle(activates=False, overlay_input="keyboard")
        backend = MemoryBackend(style=ws)
        assert backend.window_style is ws


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


class TestMultiWindowMemory:
    """create_window() + Panel(backend, window=...) on the recording backend."""

    def _make(self):
        from puikit import Panel
        from puikit.widgets import Label
        backend = MemoryBackend(80, 24)
        backend.open()
        main_panel = Panel(backend)
        main_panel.add(Label("main window"), 0, 0, 20, 1)
        main_panel.render()
        return backend, main_panel

    def test_secondary_window_renders_isolated(self):
        from puikit import Panel, WindowStyle
        from puikit.widgets import Label
        backend, _main = self._make()

        win = backend.create_window(30, 5, title="popup",
                                    style=WindowStyle(frameless=True, topmost=True))
        panel = Panel(backend, window=win)
        panel.add(Label("hello popup"), 0, 0, 20, 1)
        panel.render()

        popup_rows = ["".join(r) for r in win.snapshot()]
        main_rows = ["".join(r) for r in backend.snapshot()]
        assert any("hello popup" in r for r in popup_rows)
        assert not any("hello popup" in r for r in main_rows)
        assert any("main window" in r for r in main_rows)
        assert not any("main window" in r for r in popup_rows)
        assert win.size_units == (30.0, 5.0)
        assert win.window_style.frameless is True

    def test_main_render_unaffected_after_secondary(self):
        from puikit import Panel
        from puikit.widgets import Label
        backend, main_panel = self._make()
        win = backend.create_window(30, 5)
        panel = Panel(backend, window=win)
        panel.add(Label("popup"), 0, 0, 10, 1)
        panel.render()
        main_panel.render()   # re-render main AFTER a secondary render
        main_rows = ["".join(r) for r in backend.snapshot()]
        assert any("main window" in r for r in main_rows)
        assert not any("popup" in r for r in main_rows)

    def test_panel_auto_binds_events(self):
        from puikit import Panel
        from puikit.widgets import Button
        backend, _main = self._make()
        win = backend.create_window(30, 5)
        clicked = []
        panel = Panel(backend, window=win)
        panel.add(Button("Go", on_click=lambda: clicked.append(1)), 0, 0, 10, 1)
        panel.render()
        assert win.on_event is not None      # Panel installed itself
        from puikit import Event, EventType
        win.on_event(Event(type=EventType.MOUSE_DOWN, x=2.0, y=0.5, button="left"))
        win.on_event(Event(type=EventType.MOUSE_UP, x=2.0, y=0.5, button="left"))
        assert clicked == [1]

    def test_app_event_handler_kept(self):
        from puikit import Panel
        backend, _main = self._make()
        win = backend.create_window(30, 5)
        seen = []
        win.on_event = seen.append           # app handler set BEFORE binding
        Panel(backend, window=win)
        from puikit import Event, EventType
        event = Event(type=EventType.KEY, key="a")
        win.on_event(event)
        assert seen == [event]               # Panel did not overwrite it

    def test_lifecycle_flags(self):
        backend, _main = self._make()
        win = backend.create_window(30, 5, title="t")
        assert win.visible and not win.closed
        win.hide()
        assert not win.visible
        win.show()
        assert win.visible
        win.set_title("t2")
        assert win.title == "t2"
        win.close()
        assert win.closed and not win.visible

    def test_create_window_is_ui_thread_only(self):
        backend, _main = self._make()
        result = _run_in_thread(lambda: backend.create_window(10, 3))
        assert isinstance(result.get("error"), RuntimeError)

    def test_base_backend_raises_capability(self):
        from puikit.backend import CapabilityNotSupported
        backend = MemoryBackend()
        # a backend WITHOUT an implementation: use the Backend base method
        from puikit.backend import Backend
        with pytest.raises(CapabilityNotSupported):
            Backend.create_window(backend, 10, 3)


class TestWindowPositioning:
    """frame_px()/move_to_px(): portable top-left screen coordinates."""

    def _window(self):
        backend = MemoryBackend(80, 24)
        backend.open()
        return backend.create_window(30, 10, title="pop")

    def test_base_handle_defaults(self):
        from puikit.backend import WindowHandle
        handle = WindowHandle()
        assert handle.frame_px() is None
        handle.move_to_px(10, 20)  # base is a no-op, must not raise

    def test_memory_frame_matches_gui_default_origin(self):
        # GUI backends create secondary windows at (160, 160); the memory
        # backend mirrors that so positioning logic is testable headless.
        window = self._window()
        assert window.frame_px() == (160.0, 160.0, 30.0, 10.0)

    def test_move_to_px_round_trips_through_frame_px(self):
        window = self._window()
        window.move_to_px(415, 230)
        assert window.frame_px() == (415.0, 230.0, 30.0, 10.0)

    def test_macos_flip_math(self):
        # The AppKit conversion both macOS methods share: portable y is
        # measured from the primary screen's top edge.
        flip_h, window_h = 1080.0, 200.0
        appkit_y = 100.0                      # bottom-left origin
        portable_y = flip_h - appkit_y - window_h
        assert portable_y == 780.0
        # and back again (move_to_px inverts frame_px)
        assert flip_h - portable_y - window_h == appkit_y
