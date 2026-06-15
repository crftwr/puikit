"""Image widget tests: the GUI profile draws a real image, the TUI profile
falls back to a framed alt text — one widget, two fidelities."""

import pytest

from puikit import Event, EventType, Panel, PROFILE_GUI_DESKTOP, PROFILE_TUI, TextAttribute
from puikit.backends.memory_backend import MemoryBackend
from puikit.widgets import ImageButton, ImageView


@pytest.fixture(params=[PROFILE_TUI, PROFILE_GUI_DESKTOP], ids=["tui", "gui"])
def backend(request):
    return MemoryBackend(width=30, height=10, capabilities=request.param)


def _has_images(backend):
    return backend.capabilities.supports("images")


# --- ImageView --------------------------------------------------------------


def test_imageview_draws_image_on_gui_else_alt_fallback(backend):
    panel = Panel(backend)
    panel.add(ImageView("logo.png", alt="LOGO"), x=0, y=0, w=10, h=4)
    panel.render()
    if _has_images(backend):
        # GUI: the real image is drawn, scaled to the pane extent.
        assert len(backend.image_calls) == 1
        x, y, path, hints = backend.image_calls[0]
        assert (x, y, path) == (0, 0, "logo.png")
        assert (hints["w"], hints["h"]) == (10, 4)
    else:
        # TUI: no image call; the footprint is framed and the alt text shown.
        assert backend.image_calls == []
        joined = "\n".join(backend.snapshot())
        assert "LOGO" in joined
        assert "┌" in joined  # the placeholder frame


def test_imageview_without_alt_has_no_text_fallback(backend):
    panel = Panel(backend)
    panel.add(ImageView("logo.png"), x=0, y=0, w=8, h=3)
    panel.render()
    if not _has_images(backend):
        # Still framed, but nothing to caption it with.
        assert "┌" in "\n".join(backend.snapshot())


# --- ImageButton ------------------------------------------------------------


def test_imagebutton_fires_on_click(backend):
    panel = Panel(backend)
    clicks = []
    btn = ImageButton("ok.png", on_click=lambda: clicks.append(1), alt="OK")
    panel.add(btn, x=0, y=0, w=8, h=4)
    panel.dispatch_event(Event(type=EventType.MOUSE_CLICK, x=2, y=2, button="left"))
    assert clicks == [1]


def test_imagebutton_fires_on_activate_key(backend):
    panel = Panel(backend)
    clicks = []
    btn = ImageButton("ok.png", on_click=lambda: clicks.append(1))
    panel.add(btn, x=0, y=0, w=8, h=4)
    panel.dispatch_event(Event(type=EventType.KEY, key="enter"))
    assert clicks == [1]


def test_imagebutton_focus_ring_and_hover(backend):
    panel = Panel(backend)
    btn = ImageButton("ok.png", alt="OK")
    panel.add(btn, x=0, y=0, w=8, h=4)
    panel.render()
    # Focused: an accent box ring frames the face on every backend.
    assert "┌" in "\n".join(backend.snapshot())
    base_bg = backend.style_at(0, 0).bg
    assert base_bg == panel.theme.control_bg
    # Hover lightens the surface fill.
    panel.dispatch_event(Event(type=EventType.MOUSE_MOVE, x=3, y=2))
    panel.render()
    assert backend.style_at(7, 3).bg != base_bg


def test_imagebutton_inset_image_is_scaled(backend):
    panel = Panel(backend)
    panel.add(ImageButton("ok.png", pad=1), x=0, y=0, w=8, h=4)
    panel.render()
    if _has_images(backend):
        x, y, path, hints = backend.image_calls[0]
        # Inset by the 1-unit pad on every side.
        assert (x, y) == (1, 1)
        assert (hints["w"], hints["h"]) == (6, 2)
