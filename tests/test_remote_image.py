"""The remote-image fetch cache: URL → local file, atomic, failure-sticky."""

import io
import threading
import urllib.request

import pytest

from puikit import _remote_image


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Point the cache at a per-test directory and reset the module's
    in-process state, so tests neither see nor leave global residue."""
    monkeypatch.setattr(_remote_image, "cache_dir", lambda: str(tmp_path / "cache"))
    monkeypatch.setattr(_remote_image, "_loading", set())
    monkeypatch.setattr(_remote_image, "_failed", set())
    monkeypatch.setattr(_remote_image, "_callbacks", {})
    return tmp_path


def test_is_remote():
    assert _remote_image.is_remote("https://example.test/a.png")
    assert _remote_image.is_remote("http://example.test/a.png")
    assert not _remote_image.is_remote("docs/images/a.png")
    assert not _remote_image.is_remote("/abs/a.png")


def test_cache_path_is_stable_and_keeps_image_extension(isolated_cache):
    url = "https://example.test/img/Shot.PNG?v=2"
    assert _remote_image.cache_path(url) == _remote_image.cache_path(url)
    assert _remote_image.cache_path(url).endswith(".png")
    assert _remote_image.cache_path(url) != _remote_image.cache_path(url + "&x=1")
    # A URL with no plausible extension gets a bare digest name.
    bare = _remote_image.cache_path("https://example.test/render")
    assert "." not in bare.rsplit("/", 1)[-1]


def test_download_lands_and_later_gets_return_it(isolated_cache, monkeypatch):
    url = "https://example.test/a.png"
    body = b"\x89PNG fake bytes"
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: io.BytesIO(body))
    settled = threading.Event()

    first = _remote_image.get(url, lambda u: settled.set())
    assert first is None  # fetch just started
    assert settled.wait(5.0)

    path = _remote_image.get(url)
    assert path == _remote_image.cache_path(url)
    with open(path, "rb") as f:
        assert f.read() == body
    # Complete files only: no .part residue next to it.
    assert all("part" not in p.name for p in (isolated_cache / "cache").iterdir())


def test_failed_download_is_sticky_and_not_retried(isolated_cache, monkeypatch):
    url = "https://example.test/gone.png"
    attempts = []

    def failing(req, timeout):
        attempts.append(req.full_url)
        raise OSError("boom")

    monkeypatch.setattr(urllib.request, "urlopen", failing)
    settled = threading.Event()
    assert _remote_image.get(url, lambda u: settled.set()) is None
    assert settled.wait(5.0)
    assert _remote_image.get(url) is None
    assert _remote_image.get(url) is None
    assert attempts == [url]  # one attempt, then remembered as failed


def test_same_callback_registered_once(isolated_cache, monkeypatch):
    url = "https://example.test/slow.png"
    release = threading.Event()
    calls = []

    def slow(req, timeout):
        release.wait(5.0)
        return io.BytesIO(b"data")

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    done = threading.Event()

    def on_done(u):
        calls.append(u)
        done.set()

    # The layout pass re-requests the same URL with the same callback.
    assert _remote_image.get(url, on_done) is None
    assert _remote_image.get(url, on_done) is None
    release.set()
    assert done.wait(5.0)
    assert calls == [url]
