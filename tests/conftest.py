import sys
import threading
import time

import pytest


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    # pyproject picks pytest-timeout's signal method (it interrupts even a
    # pure-Python busy loop and names the hung test), but SIGALRM does not
    # exist on Windows -- there the plugin raises AttributeError before the
    # first test runs. Fall back to the thread method, keeping the fail-safe
    # timeout everywhere the suite runs. (trylast: the plugin's own
    # pytest_configure must have stored _env_timeout_method first.)
    if sys.platform == "win32" and getattr(config, "_env_timeout_method", None) == "signal":
        config._env_timeout_method = "thread"


class Inbox:
    """Stands in for a backend's post-to-UI-thread (AppHelper.callAfter, call_on_main_thread): collects what the worker posts, and
    the test runs it on its own thread, as the UI thread would."""

    def __init__(self):
        self.items = []
        self.cond = threading.Condition()

    def __call__(self, fn, *args):
        with self.cond:
            self.items.append((fn, args))
            self.cond.notify_all()

    def run(self, count, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.items) < count:
                left = deadline - time.monotonic()
                assert left > 0, "worker did not report back"
                self.cond.wait(left)
            items, self.items = self.items, []
        for fn, args in items:
            fn(*args)
