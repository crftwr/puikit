"""Where puikit._ghostty looks for libghostty-vt: _bin/ inside the package
(what an application bundle ships), then the system library path - nothing
per-user such as a cache under the home folder, and no environment variable."""

import os

import pytest

from puikit import _ghostty


@pytest.fixture
def tried(monkeypatch, tmp_path):
    """Load with every candidate present, recording the order they are tried
    in; each one fails, so all are tried."""
    paths = []

    def fake_cdll(path):
        paths.append(path)
        raise OSError("not really a library")

    monkeypatch.setattr(_ghostty, "_lib", None)
    monkeypatch.setattr(_ghostty, "_load_error", None)
    monkeypatch.setattr(_ghostty.C, "CDLL", fake_cdll)
    monkeypatch.setattr(_ghostty.os.path, "exists", lambda p: True)
    monkeypatch.setattr(_ghostty.ctypes.util, "find_library", lambda name: "/usr/lib/sys-ghostty")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return paths


def test_order_is_the_package_then_the_system(tried, monkeypatch):
    monkeypatch.setenv("PUIKIT_LIBGHOSTTY", "/opt/named/libghostty-vt")  # ignored
    assert _ghostty.load() is None
    bundled = [os.path.join(_ghostty.bundled_dir(), n) for n in _ghostty._lib_names()]
    assert tried == [*bundled, "/usr/lib/sys-ghostty"]


def test_no_per_user_location_is_searched(tried, monkeypatch, tmp_path):
    _ghostty.load()
    assert not any(str(tmp_path) in p or ".cache" in p for p in tried)


def test_the_bundled_dir_is_inside_the_package():
    pkg = os.path.dirname(os.path.abspath(_ghostty.__file__))
    assert _ghostty.bundled_dir() == os.path.join(pkg, "_bin")


# --- the version the binding is written for -------------------------------------

@pytest.mark.parametrize("found, ok", [
    ("1.0.1", True),             # the release it is written for
    ("1.0.2", True),             # newer, same major: upstream's promise
    ("1.3.0-dev+abc1234", True),  # a development build towards a newer minor
    ("1.0.0", False),            # older than the binding
    ("2.0.0", False),            # another major may break it
    ("", False),
    ("not a version", False),
])
def test_compatible_follows_upstreams_promise(found, ok):
    assert _ghostty.compatible(found, "1.0.1") is ok


class _FakeLib:
    def __init__(self, version):
        self._version = version

        def ghostty_version():
            return self._version
        self.ghostty_version = ghostty_version


def test_a_library_of_another_version_is_refused_with_the_reason(monkeypatch):
    monkeypatch.setattr(_ghostty, "_lib", None)
    monkeypatch.setattr(_ghostty, "_load_error", None)
    monkeypatch.setattr(_ghostty.os.path, "exists", lambda p: True)
    monkeypatch.setattr(_ghostty.ctypes.util, "find_library", lambda name: None)
    monkeypatch.setattr(_ghostty.C, "CDLL", lambda path: _FakeLib(b"2.0.0"))
    bound = []
    monkeypatch.setattr(_ghostty, "_bind", bound.append)
    assert _ghostty.load() is None
    assert not bound  # never bound against a library it does not fit
    assert "libghostty-vt 2.0.0" in _ghostty.load_error()
    assert _ghostty.LIBGHOSTTY_VERSION in _ghostty.load_error()


def test_the_fetch_script_pins_the_declared_version():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "fetch_libghostty.py")
    spec = importlib.util.spec_from_file_location("fetch_libghostty", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.declared_version() == _ghostty.LIBGHOSTTY_VERSION
    assert set(mod.PINS[_ghostty.LIBGHOSTTY_VERSION]) == {"macos", "windows-x64", "windows-arm64"}
