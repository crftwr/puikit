"""Rich-clipboard capability: a widget always calls ``set_clipboard_rich`` and
gets working plain-text copy everywhere, while a backend that advertises
``clipboard_rich`` keeps the extra representations (HTML / RTF)."""

import sys

import pytest

from puikit import Panel, PROFILE_GUI_DESKTOP
from puikit.backends.memory_backend import MemoryBackend


def test_default_set_clipboard_rich_falls_back_to_plain():
    # The base backend (no clipboard_rich) drops the rich reps and keeps the text,
    # so a plain paste still works and the widget never has to branch.
    be = MemoryBackend(width=10, height=4)
    be.set_clipboard_rich("plain text", html="<b>plain text</b>")
    assert be.get_clipboard() == "plain text"


def test_panel_passes_rich_reps_through_to_backend():
    captured = {}

    class RichBackend(MemoryBackend):
        def set_clipboard_rich(self, text, *, html=None, rtf=None):
            captured.update(text=text, html=html, rtf=rtf)
            self.set_clipboard(text)

    be = RichBackend(width=10, height=4, capabilities=PROFILE_GUI_DESKTOP)
    panel = Panel(be)
    panel.set_clipboard_rich("hi", html="<i>hi</i>", rtf="{\\rtf hi}")
    assert captured == {"text": "hi", "html": "<i>hi</i>", "rtf": "{\\rtf hi}"}
    assert be.get_clipboard() == "hi"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only backend")
def test_macos_backend_advertises_clipboard_rich():
    pytest.importorskip("AppKit", reason="pyobjc not installed")
    from puikit.backends.macos_backend import MacOSBackend

    assert MacOSBackend.PROFILE.supports("clipboard_rich")
