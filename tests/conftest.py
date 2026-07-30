import sys

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
