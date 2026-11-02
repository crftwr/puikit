"""PuiKit's dependencies resolve as intended on each platform.

Pillow is wanted wherever it has wheels, and left out elsewhere, where pip
would have to build it and an install would fail. A wrong marker would
silently drop terminal pictures on a main platform, so the main ones are
listed here, with the machine names Python reports on them.
"""

import pathlib

import pytest

tomllib = pytest.importorskip("tomllib")  # Python 3.11+
requirements = pytest.importorskip("packaging.requirements")

_PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"


def _dependencies():
    with open(_PYPROJECT, "rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    return {r.name.lower(): r for r in map(requirements.Requirement, deps)}


def test_windows_curses_is_not_a_dependency():
    # It has no arm64 wheel and no sdist, so requiring it made PuiKit
    # uninstallable on native ARM64 Python on Windows.
    assert "windows-curses" not in _dependencies()


def _env(sys_platform, machine):
    return {"sys_platform": sys_platform, "platform_machine": machine}


@pytest.mark.parametrize("sys_platform, machine", [
    ("darwin", "arm64"), ("darwin", "x86_64"),
    ("linux", "x86_64"), ("linux", "aarch64"),
    ("win32", "AMD64"), ("win32", "ARM64"), ("win32", "x86"),
])
def test_pillow_is_installed_where_it_has_wheels(sys_platform, machine):
    assert _dependencies()["pillow"].marker.evaluate(_env(sys_platform, machine))


@pytest.mark.parametrize("sys_platform, machine", [
    *[("linux", m) for m in ("armv7l", "armv6l", "i686", "ppc64le", "s390x", "riscv64")],
    # A machine name with wheels elsewhere, on an OS without them.
    ("cygwin", "x86_64"), ("msys", "x86_64"), ("freebsd14", "arm64"), ("freebsd14", "amd64"),
    ("darwin", "aarch64"), ("linux", "AMD64"),
])
def test_pillow_is_left_out_where_pip_would_build_it(sys_platform, machine):
    assert not _dependencies()["pillow"].marker.evaluate(_env(sys_platform, machine))
