"""A UI-thread stall detector.

An app that does slow work on the UI thread does not crash — it stops
repainting, and the only report anyone can file is "it froze for a moment".
This module turns that into a log line naming the call stack that was running,
so the offending call site is found by reading the log instead of by bisecting
the app one operation at a time.

How it works: the UI thread brackets each unit of work it does — one event
delivered to the app's handler, one animation-tick frame, one ``call_later``
callback — with :func:`enter` / :func:`leave`, which between them cost two
attribute writes. A daemon sampling thread watches that timestamp, and once a
unit has been running longer than the threshold it dumps the UI thread's Python
stack (``sys._current_frames``) into the log. A long stall is reported again as
it grows, so a stack that moved is visible; when the unit finally finishes, its
total is logged too.

Off unless ``PUIKIT_UI_WATCHDOG`` is set — ``1`` for the default 250 ms
threshold, or a number of milliseconds (``PUIKIT_UI_WATCHDOG=500``). Nothing is
allocated and no thread is started while it is off, and the brackets return on
their first line, so the detector can be left wired into the loop permanently.

Time spent inside a **nested OS loop the user is driving** — a native menu
tracking session, an OS drag, a shell-out to a full-screen child — is not a
stall: the app is not painting because the user is doing something else. Those
sites hold :func:`paused` and the clock restarts when they return.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from typing import Any, Callable

_logger = logging.getLogger(__name__)

#: Threshold used by ``PUIKIT_UI_WATCHDOG=1``. A frame is 16 ms, so a quarter of
#: a second is already several dropped frames — visible as a stutter — while
#: staying above the one-off costs (a first render, a font load) that would make
#: a lower default cry wolf on every startup.
_DEFAULT_THRESHOLD_MS = 250.0
#: The sampler wakes at a quarter of the threshold, so a stall is caught close to
#: the moment it crosses rather than up to a full threshold late; clamped so a
#: tiny threshold cannot spin the sampler and a huge one still checks now and then.
_SAMPLE_FRACTION = 0.25
_MIN_SAMPLE_S = 0.02
_MAX_SAMPLE_S = 0.5
#: A stall that keeps going is re-reported each time it grows this many times
#: longer, so a 30 s freeze produces a handful of stacks (did it move? where is
#: it now?) rather than one per sample.
_ESCALATION = 4.0
#: Innermost frames kept per report. The interesting call is at the bottom of the
#: stack; the app's whole event-dispatch preamble above it is noise, and the log
#: pane this lands in is a few lines tall.
_STACK_FRAMES = 12

_OFF_VALUES = frozenset({"", "0", "off", "no", "false"})
_ON_VALUES = frozenset({"1", "on", "yes", "true"})


def _threshold_from_env() -> float | None:
    """The configured threshold in seconds, or None when the watchdog is off.

    Read at :func:`install` time rather than at import, so an app that turns the
    detector on from its own command line (setting the variable in ``main``,
    long after ``import puikit``) is honored."""
    raw = os.environ.get("PUIKIT_UI_WATCHDOG", "").strip().lower()
    if raw in _OFF_VALUES:
        return None
    if raw in _ON_VALUES:
        return _DEFAULT_THRESHOLD_MS / 1000.0
    try:
        ms = float(raw)
    except ValueError:
        _logger.warning(
            "PUIKIT_UI_WATCHDOG=%r is neither a switch nor a number of "
            "milliseconds; using %g ms", raw, _DEFAULT_THRESHOLD_MS)
        return _DEFAULT_THRESHOLD_MS / 1000.0
    if ms <= 0:
        return None
    return ms / 1000.0


def _format_duration(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms" if seconds < 1.0 else f"{seconds:.1f} s"


class _Watchdog:
    """Watches one thread. Created by :func:`install`; there is one at a time."""

    def __init__(self, threshold: float, thread: threading.Thread):
        self.threshold = threshold
        self.ident = thread.ident
        self._thread = thread
        #: When the unit of work now running began, or 0.0 while the UI thread is
        #: idle (or paused). Written by the UI thread, read by the sampler; a
        #: single float, so a torn read is impossible and no lock is needed.
        self._busy_since = 0.0
        self._label = ""
        #: Nesting depth: a native menu pumps events inside the dispatch that
        #: opened it, so brackets do nest. Only the outermost one runs the clock.
        self._depth = 0
        #: Elapsed time already reported for the current unit, driving escalation.
        self._reported = 0.0
        self._stop = threading.Event()
        self._sampler = threading.Thread(
            target=self._sample_loop, name="puikit-ui-watchdog", daemon=True)

    # --- UI-thread side (hot path) -------------------------------------------

    def enter(self, label: str) -> None:
        if threading.get_ident() != self.ident:
            # A backend that pumps its ticks off the UI thread would otherwise
            # corrupt the depth count and time the wrong thread's work.
            return
        if self._depth == 0:
            self._label = label
            self._reported = 0.0
            self._busy_since = time.monotonic()
        self._depth += 1

    def leave(self) -> None:
        if threading.get_ident() != self.ident or self._depth == 0:
            return
        self._depth -= 1
        if self._depth:
            return
        since, self._busy_since = self._busy_since, 0.0
        if self._reported and since:
            _logger.warning("UI thread unblocked after %s in %s",
                            _format_duration(time.monotonic() - since), self._label)

    @contextmanager
    def paused(self):
        """Stop the clock for a nested OS loop, then restart it on the way out.

        Restart rather than resume: the time the user spent holding a menu open
        is not part of whatever the app does next, and carrying it forward would
        report the menu's duration against the code after it."""
        was_busy = self._busy_since
        self._busy_since = 0.0
        try:
            yield
        finally:
            if was_busy and self._depth:
                self._busy_since = time.monotonic()
                self._reported = 0.0

    # --- sampler side ---------------------------------------------------------

    def start(self) -> None:
        self._sampler.start()

    def stop(self) -> None:
        self._stop.set()

    def _sample_loop(self) -> None:
        interval = max(_MIN_SAMPLE_S,
                       min(self.threshold * _SAMPLE_FRACTION, _MAX_SAMPLE_S))
        while not self._stop.wait(interval):
            if not self._thread.is_alive():
                return
            since = self._busy_since
            if not since:
                continue
            elapsed = time.monotonic() - since
            due = self._reported * _ESCALATION if self._reported else self.threshold
            if elapsed < due:
                continue
            label = self._label
            if self._busy_since != since:
                # The unit finished while we were measuring it — reporting now
                # would name a stack the UI thread has already left.
                continue
            self._reported = elapsed
            self._report(elapsed, label)

    def _report(self, elapsed: float, label: str) -> None:
        _logger.warning("UI thread blocked %s in %s", _format_duration(elapsed),
                        label or "unknown work")
        for line in self._ui_stack():
            _logger.warning("  %s", line)

    def _ui_stack(self) -> list[str]:
        """The UI thread's innermost frames, one preformatted line each.

        One record per frame rather than one multi-line record: these land in an
        app's log pane, which lists records — a single record with newlines in it
        is one unreadable row."""
        current_frames = getattr(sys, "_current_frames", None)
        frame = current_frames().get(self.ident) if current_frames else None
        if frame is None:
            # Not CPython, or the thread is gone between the sample and here.
            return ["(no stack available for the UI thread)"]
        try:
            stack = traceback.extract_stack(frame)
        except Exception as exc:  # pragma: no cover - defensive
            return [f"(stack unavailable: {exc})"]
        lines = []
        if len(stack) > _STACK_FRAMES:
            lines.append(f"... {len(stack) - _STACK_FRAMES} outer frames")
        for f in stack[-_STACK_FRAMES:]:
            where = os.path.join(os.path.basename(os.path.dirname(f.filename)),
                                 os.path.basename(f.filename))
            text = f"  {f.line}" if f.line else ""
            lines.append(f"{where}:{f.lineno} in {f.name}{text}")
        return lines


#: The installed watchdog, or None while the detector is off. Read on the hot
#: path, so the "off" check is one global load and an ``is None``.
_active: _Watchdog | None = None


def install(thread: threading.Thread) -> None:
    """Start watching ``thread`` as the UI thread, if the environment asks for it.

    Called from every backend's ``open()`` (via ``Backend._note_ui_thread``), so
    the detector follows the thread that actually runs the loop. Idempotent for a
    given thread; re-opening on another thread hands the watch over."""
    global _active
    if _active is not None:
        if _active.ident == thread.ident:
            return
        _active.stop()
        _active = None
    threshold = _threshold_from_env()
    if threshold is None:
        return
    _active = _Watchdog(threshold, thread)
    _active.start()
    _logger.info("UI-thread watchdog on: reporting stalls over %s",
                 _format_duration(threshold))


def uninstall() -> None:
    """Stop the sampler and forget the watched thread (tests; process teardown)."""
    global _active
    if _active is not None:
        _active.stop()
        _active = None


def enter(label: str) -> None:
    """Mark the start of a unit of UI-thread work. Pair with :func:`leave`."""
    watchdog = _active
    if watchdog is not None:
        watchdog.enter(label)


def leave() -> None:
    """Mark the end of the unit :func:`enter` began."""
    watchdog = _active
    if watchdog is not None:
        watchdog.leave()


@contextmanager
def paused():
    """Don't count the enclosed nested OS loop against the UI thread."""
    watchdog = _active
    if watchdog is None:
        yield
        return
    with watchdog.paused():
        yield


def _event_label(event: Any) -> str:
    kind = getattr(getattr(event, "type", None), "value", None) or "event"
    key = getattr(event, "key", None)
    return f"{kind} {key!r}" if key else kind


def wrap_handler(handler: Callable) -> Callable:
    """``handler``, bracketed so each event it handles is timed.

    Returns the handler itself while the watchdog is off, so a loop that wraps
    on every iteration pays nothing. Already-wrapped handlers pass through: the
    backends whose ``run_event_loop`` delegates to ``run_event_loop_iteration``
    would otherwise wrap twice and label the inner unit."""
    if _active is None or getattr(handler, "_puikit_watched", False):
        return handler

    def watched(event) -> None:
        enter(_event_label(event))
        try:
            handler(event)
        finally:
            leave()

    watched._puikit_watched = True  # type: ignore[attr-defined]
    return watched


def wrap_callback(callback: Callable, label: str) -> Callable:
    """``callback``, bracketed under ``label`` — for a timer callback the app
    hands a backend, which runs on the UI thread outside any event dispatch."""
    if _active is None or getattr(callback, "_puikit_watched", False):
        return callback

    def watched(*args, **kwargs):
        enter(label)
        try:
            return callback(*args, **kwargs)
        finally:
            leave()

    watched._puikit_watched = True  # type: ignore[attr-defined]
    return watched
