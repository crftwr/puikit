"""The UI-thread stall detector: what it reports, and what it refuses to."""

import logging
import sys
import threading
import time

import pytest

from puikit import _watchdog
from puikit.backends.memory_backend import MemoryBackend
from puikit.event import Event, EventType

#: Short enough to keep the suite quick, long enough that a loaded CI machine
#: does not cross it by accident between two adjacent statements.
_THRESHOLD_MS = 60
#: A block that is unambiguously over the threshold, plus a sampling interval.
_STALL_S = 0.35


@pytest.fixture
def watching(monkeypatch):
    """Install the watchdog on this thread and take it back out afterwards."""
    monkeypatch.setenv("PUIKIT_UI_WATCHDOG", str(_THRESHOLD_MS))
    _watchdog.install(threading.current_thread())
    yield
    _watchdog.uninstall()


def _reports(caplog):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("UI thread blocked")]


# --- configuration ---------------------------------------------------------

@pytest.mark.parametrize("value", ["", "0", "off", "no", "false"])
def test_off_by_default_and_by_switch(monkeypatch, value):
    monkeypatch.setenv("PUIKIT_UI_WATCHDOG", value)
    assert _watchdog._threshold_from_env() is None
    monkeypatch.delenv("PUIKIT_UI_WATCHDOG")
    assert _watchdog._threshold_from_env() is None


def test_switch_and_millisecond_forms(monkeypatch):
    monkeypatch.setenv("PUIKIT_UI_WATCHDOG", "1")
    assert _watchdog._threshold_from_env() == pytest.approx(0.25)
    monkeypatch.setenv("PUIKIT_UI_WATCHDOG", "500")
    assert _watchdog._threshold_from_env() == pytest.approx(0.5)


def test_unreadable_value_falls_back_rather_than_raising(monkeypatch):
    # A typo in a debugging session must not stop the app from starting.
    monkeypatch.setenv("PUIKIT_UI_WATCHDOG", "yesplease")
    assert _watchdog._threshold_from_env() == pytest.approx(0.25)


def test_nothing_runs_while_off(monkeypatch):
    monkeypatch.delenv("PUIKIT_UI_WATCHDOG", raising=False)
    before = threading.active_count()
    _watchdog.install(threading.current_thread())
    try:
        handler = lambda event: None
        assert _watchdog.wrap_handler(handler) is handler
        assert threading.active_count() == before
        # The brackets are still called from the loop; they must be inert.
        _watchdog.enter("key event")
        _watchdog.leave()
    finally:
        _watchdog.uninstall()


# --- reporting -------------------------------------------------------------

def test_reports_a_stall_with_the_stack_it_was_in(watching, caplog):
    def slow_work_under_test():
        time.sleep(_STALL_S)

    with caplog.at_level(logging.WARNING, logger="puikit._watchdog"):
        _watchdog.enter("key 'f5'")
        slow_work_under_test()
        _watchdog.leave()

    reports = _reports(caplog)
    assert reports, "a block well past the threshold went unreported"
    assert "key 'f5'" in reports[0]
    frames = "\n".join(r.getMessage() for r in caplog.records)
    assert "slow_work_under_test" in frames, "the report names no call site"
    assert any(r.getMessage().startswith("UI thread unblocked after")
               for r in caplog.records), "no total was logged when it ended"


def test_work_under_the_threshold_is_not_reported(watching, caplog):
    with caplog.at_level(logging.WARNING, logger="puikit._watchdog"):
        _watchdog.enter("mouse_move")
        _watchdog.leave()
        time.sleep(_STALL_S)
    assert not _reports(caplog), "an idle UI thread was reported as blocked"


def test_a_nested_os_loop_is_not_a_stall(watching, caplog):
    # A native menu the user is reading, a shell-out to an editor: the UI thread
    # is held, but by the user, and reporting it would bury the real stalls.
    with caplog.at_level(logging.WARNING, logger="puikit._watchdog"):
        _watchdog.enter("mouse_click")
        with _watchdog.paused():
            time.sleep(_STALL_S)
        _watchdog.leave()
    assert not _reports(caplog)


def test_the_clock_restarts_after_a_nested_loop(watching, caplog):
    # Restart, not resume: work done after the menu closes is timed on its own.
    with caplog.at_level(logging.WARNING, logger="puikit._watchdog"):
        _watchdog.enter("mouse_click")
        with _watchdog.paused():
            time.sleep(_STALL_S)
        time.sleep(_STALL_S)
        _watchdog.leave()
    reports = _reports(caplog)
    assert reports
    # Only the work after the pause is counted, never the pause plus the work.
    worst_ms = max(float(r.split()[3]) for r in reports)
    assert worst_ms < _STALL_S * 2 * 1000


def test_only_the_ui_thread_is_timed(watching, caplog):
    # A backend that pumps its ticks off the UI thread would otherwise corrupt
    # the depth count and report the wrong thread's work.
    def worker():
        _watchdog.enter("animation tick")
        time.sleep(_STALL_S)
        _watchdog.leave()

    with caplog.at_level(logging.WARNING, logger="puikit._watchdog"):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
    assert not _reports(caplog)


def test_nested_brackets_are_timed_as_one_unit(watching, caplog):
    with caplog.at_level(logging.WARNING, logger="puikit._watchdog"):
        _watchdog.enter("key 'enter'")
        _watchdog.enter("mouse_click")  # a native menu pumping inside a dispatch
        _watchdog.leave()
        time.sleep(_STALL_S)
        _watchdog.leave()
    reports = _reports(caplog)
    # The inner leave() must not end the unit, nor rename it.
    assert reports
    assert all("key 'enter'" in report for report in reports)


def test_a_long_stall_is_reported_again_as_it_grows(watching, caplog):
    # One stack at the threshold can be misleading (a stall that moves elsewhere
    # looks like it is still where it started), so a stall that keeps going is
    # re-reported each time it grows several times longer.
    with caplog.at_level(logging.WARNING, logger="puikit._watchdog"):
        _watchdog.enter("key 'f5'")
        time.sleep(_THRESHOLD_MS / 1000 * 12)
        _watchdog.leave()
    reports = _reports(caplog)
    assert len(reports) > 1
    elapsed = [float(r.split()[3]) for r in reports]
    assert elapsed == sorted(elapsed)
    assert len(reports) < 8, "a long freeze must not report once per sample"


def test_every_backend_open_notes_the_ui_thread():
    """The watchdog's subject — and _assert_ui_thread's — comes from open().

    VTBackend never called it, so on the TUI (where it is the default) the
    stall detector had nothing to watch and the UI-thread contract went
    unenforced. A backend added later can make the same omission silently, so
    this checks the source of every open() we can import here."""
    import inspect

    from puikit.backends.curses_backend import CursesBackend
    from puikit.backends.vt_backend import VTBackend
    from puikit.backends.web_backend import WebBackend

    backends = [VTBackend, CursesBackend, WebBackend, MemoryBackend]
    if sys.platform == "darwin":
        from puikit.backends.macos_backend import MacOSBackend
        backends.append(MacOSBackend)
    elif sys.platform == "win32":
        from puikit.backends.windows_backend import WindowsBackend
        backends.append(WindowsBackend)

    for backend in backends:
        assert "_note_ui_thread()" in inspect.getsource(backend.open), backend.__name__


# --- the loop's side of it -------------------------------------------------

def test_handler_wrapping_is_idempotent(watching):
    handler = lambda event: None
    once = _watchdog.wrap_handler(handler)
    assert once is not handler
    # run_event_loop delegating to run_event_loop_iteration wraps twice.
    assert _watchdog.wrap_handler(once) is once


def test_a_slow_event_handler_is_caught_end_to_end(monkeypatch, caplog):
    monkeypatch.setenv("PUIKIT_UI_WATCHDOG", str(_THRESHOLD_MS))
    backend = MemoryBackend(80, 24)
    backend.open()  # notes the UI thread, which installs the watchdog
    try:
        backend.feed_event(Event(type=EventType.KEY, key="f5"))
        with caplog.at_level(logging.WARNING, logger="puikit._watchdog"):
            backend.run_event_loop(lambda event: time.sleep(_STALL_S))
        reports = _reports(caplog)
        assert reports and "f5" in reports[0]
    finally:
        _watchdog.uninstall()
        backend.close()
