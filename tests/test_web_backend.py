"""WebBackend tests that run without a browser.

The transport round-trip uses a tiny hand-rolled WebSocket client (a raw socket
+ the RFC 6455 handshake and masked framing) so the server's hand-rolled framing
is exercised both ways, headless.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import time

import pytest

from puikit import Font, FontSlant, FontWeight, Panel, Style, TextAttribute
from puikit.backends import create_backend
from puikit.backends import _ttf
from puikit.backends._web_server import WebServer
from puikit.backends.web_backend import (
    PROFILE_WEB,
    WebBackend,
    _css_color,
    translate_key,
)
from puikit.event import EventType
from puikit.widgets import Button, Label

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "puikit", "fonts")


# --- TrueType metrics reader ------------------------------------------------


def test_ttf_mono_is_fixed_advance():
    mono = _ttf.load(os.path.join(_FONT_DIR, "NotoSansMono-Regular.ttf"))
    advances = {round(mono.advance(ord(c)), 6) for c in "MilW10@.,;xyz"}
    assert len(advances) == 1  # every glyph shares one advance -> monospaced
    assert mono.units_per_em > 0
    assert mono.line_height > 1.0  # ascent + descent + gap, in em


def test_ttf_proportional_varies():
    sans = _ttf.load(os.path.join(_FONT_DIR, "NotoSans-Regular.ttf"))
    assert sans.advance(ord("i")) < sans.advance(ord("W"))
    # A summed run equals the sum of its glyph advances (no kerning).
    total = sans.advance(ord("H")) + sans.advance(ord("i"))
    assert sans.advance_text("Hi") == pytest.approx(total)


def test_ttf_cache_returns_same_object():
    a = _ttf.load(os.path.join(_FONT_DIR, "NotoSans-Bold.ttf"))
    b = _ttf.load(os.path.join(_FONT_DIR, "NotoSans-Bold.ttf"))
    assert a is b


# --- measurement seam -------------------------------------------------------


def _backend() -> WebBackend:
    b = WebBackend(open_browser=False)
    b._canvas_px = (800, 600)  # pretend a tab connected at 800x600 CSS px
    return b


def test_grid_font_measures_in_whole_columns():
    b = _backend()
    assert b.measure_text("Hello") == pytest.approx(5.0)
    assert b.measure_text("") == pytest.approx(0.0)
    # The base grid font is exactly one base unit tall by definition.
    assert b.measure_line_height() == pytest.approx(1.0)
    fm = b.font_metrics()
    assert fm.ascent + fm.descent == pytest.approx(1.0)
    assert fm.ascent > 0 and fm.descent > 0


def test_proportional_narrower_than_grid():
    b = _backend()
    prop = Style(font=Font())  # proportional UI face
    assert b.measure_text("Hello", prop) < b.measure_text("Hello")


def test_measure_scales_with_font_size():
    b = _backend()
    small = Style(font=Font(size=10))
    big = Style(font=Font(size=20))
    assert b.measure_text("Widgets", big) == pytest.approx(
        2 * b.measure_text("Widgets", small), rel=1e-3
    )
    assert b.measure_font_size(big) == 20


def test_missing_glyphs_measured_by_em_width():
    # Noto Sans/Mono lack CJK glyphs; the browser draws them from a fallback at
    # ~1 em. Measuring them as the .notdef box (too narrow) or the terminal
    # column count of 2 (too wide, ~1.67 em) both mis-wrapped Japanese; the em
    # width is the right estimate.
    b = _backend()
    one_em = b._base_pt / b._base_w  # one em of the base grid face, in base units
    one_wide = b.measure_text("あ")
    assert 1.0 < one_wide < 2.0             # wider than a mono cell, narrower than 2
    assert one_wide == pytest.approx(one_em)
    assert b.measure_text("ああああ") == pytest.approx(4 * one_wide)
    assert b.measure_text("aあ") == pytest.approx(1.0 + one_wide)  # latin cell + em
    assert b.measure_text("abc") == pytest.approx(3.0)             # latin unaffected


def test_geometry_from_canvas_size():
    b = _backend()
    bw, bh = b.base_pixel_size
    assert b.size == (int(800 / bw), int(600 / bh))
    assert b.size_units[0] == pytest.approx(800 / bw)
    # Before a tab connects, the seed size is reported.
    fresh = WebBackend(width=42, height=13, open_browser=False)
    assert fresh.size == (42, 13)


# --- keyboard contract ------------------------------------------------------


def _key(key, **mods):
    e = translate_key(key, mods)
    return None if e is None else (e.key, e.char, e.modifiers)


def test_translate_key_letters_keep_case_rule():
    assert _key("a") == ("a", "a", frozenset())
    # Shift+A: key lowercased, Shift kept (distinct from plain "a").
    assert _key("A", shift=True) == ("a", "A", frozenset({"shift"}))


def test_translate_key_shifted_symbol_drops_shift():
    # Shift+1 -> "!": the glyph is the identity and Shift is dropped.
    assert _key("!", shift=True) == ("!", "!", frozenset())


def test_translate_key_named_and_modifiers():
    assert _key("ArrowUp") == ("up", None, frozenset())
    assert _key("Enter") == ("enter", None, frozenset())
    assert _key("F5") == ("f5", None, frozenset())
    assert _key(" ") == ("space", " ", frozenset())
    # Meta maps to the "cmd" command modifier (matching the macOS backend).
    assert _key("c", meta=True) == ("c", "c", frozenset({"cmd"}))
    assert _key("c", ctrl=True) == ("c", "c", frozenset({"ctrl"}))


def test_translate_key_ignores_bare_modifiers():
    assert translate_key("Shift", {"shift": True}) is None
    assert translate_key("Meta", {"meta": True}) is None


# --- mouse events -----------------------------------------------------------


def test_mouse_down_up_move_drag():
    b = _backend()
    bw, bh = b.base_pixel_size
    down = b._mouse_event({"kind": "down", "x": 2 * bw, "y": 3 * bh, "button": "left", "mods": {}})
    assert down.type is EventType.MOUSE_DOWN
    assert down.x == pytest.approx(2.0) and down.y == pytest.approx(3.0)
    # A move while a button is held is a drag; released, it is a plain move.
    drag = b._mouse_event({"kind": "move", "x": 0, "y": 0, "mods": {}})
    assert drag.type is EventType.MOUSE_DRAG
    b._mouse_event({"kind": "up", "x": 0, "y": 0, "button": "left", "mods": {}})
    move = b._mouse_event({"kind": "move", "x": 0, "y": 0, "mods": {}})
    assert move.type is EventType.MOUSE_MOVE


def test_mouse_scroll_direction_and_units():
    b = _backend()
    down = b._mouse_event({"kind": "scroll", "x": 0, "y": 0, "dy": 10, "mods": {}})
    up = b._mouse_event({"kind": "scroll", "x": 0, "y": 0, "dy": -10, "mods": {}})
    assert down.scroll == -1 and up.scroll == 1  # browser deltaY is +down
    assert down.hints["scroll_units"] < 0 < up.hints["scroll_units"]


# --- capability profile -----------------------------------------------------


def test_profile_is_web_gui_with_v1_overrides():
    caps = PROFILE_WEB
    assert caps.supports("pixel_layout")
    assert caps.supports("fonts") and caps.supports("proportional_text")
    assert caps.supports("vector_shapes") and caps.supports("images")
    assert caps.supports("transparency") and caps.supports("shadow")
    assert caps.supports("hover") and caps.supports("pointer_shape")
    assert caps.supports("ime")  # composition via a hidden page <input>
    # Deferred axes are advertised off so the Panel substitutes its fallbacks.
    assert not caps.supports("animation")
    assert caps.supports("animation_ticks")
    assert not caps.supports("icons")


def test_factory_aliases():
    for name in ("web", "webbrowser", "browser"):
        b = create_backend(name, open_browser=False)
        assert isinstance(b, WebBackend)


# --- frame serialization ----------------------------------------------------


def _ops_after(draw):
    b = _backend()
    b.clear()
    draw(b)
    return b._serialize(b._back)


def test_serialize_fill_and_text_to_pixels():
    b = _backend()
    bw, bh = b.base_pixel_size
    b.clear()
    b.fill_rect(0, 0, 10, 2, Style(bg=(10, 20, 30)))
    b.draw_text(1, 1, "Hi", Style(fg=(255, 0, 0)))
    ops = b._serialize(b._back)
    assert ops[0] == ["fill", 0.0, 0.0, 10 * bw, 2 * bh, "rgba(10,20,30,1.000)"]
    text = ops[-1]
    assert text[0] == "text"
    assert text[1] == pytest.approx(1 * bw)          # x in px
    assert text[3] == "Hi" and text[5] == "rgba(255,0,0,1.000)"


def test_serialize_reverse_swaps_and_dim_alpha():
    ops = _ops_after(
        lambda b: b.draw_text(0, 0, "x", Style(fg=(255, 255, 255), bg=(0, 0, 0),
                                               attr=TextAttribute.REVERSE))
    )
    # Reverse: the background band is painted with the original fg (white).
    fill = [o for o in ops if o[0] == "fill"][0]
    assert fill[-1] == "rgba(255,255,255,1.000)"
    dim = _ops_after(lambda b: b.draw_text(0, 0, "x", Style(fg=(200, 200, 200),
                                                            attr=TextAttribute.DIM)))
    assert dim[-1][5].endswith("0.550)")  # dimmed foreground alpha


def test_serialize_vector_faces():
    checks = _ops_after(lambda b: b.draw_check(0, 0, 2, 1, Style(fg=(1, 2, 3))))
    assert checks[0][0] == "check"
    chevrons = _ops_after(lambda b: b.draw_chevron(0, 0, 2, 1, True, Style(fg=(1, 2, 3))))
    assert chevrons[0][0] == "chevron" and chevrons[0][-2] is True
    rrect = _ops_after(lambda b: b.draw_round_rect(0, 0, 4, 2, 3.0,
                                                   Style(bg=(9, 9, 9)), hints={"fill": True}))
    assert rrect[0][0] == "rrect" and rrect[0][5] == 3.0


def test_serialize_shadow_radius_passthrough():
    # A bare modal (square draw_box panel) casts with radius=None -> the client
    # must draw a SQUARE silhouette, not a pill (draw_shadow's contract), so the
    # op carries None; a rounded panel passes a real radius through unchanged.
    square = _ops_after(lambda b: b.draw_shadow(0, 0, 20, 8))
    assert square[0][0] == "shadow" and square[0][5] is None
    rounded = _ops_after(lambda b: b.draw_shadow(0, 0, 20, 8, radius=8.0))
    assert rounded[0][5] == 8.0


def test_serialize_clip_pair_and_scrollbar():
    ops = _ops_after(lambda b: (b.push_clip(0, 0, 4, 4), b.pop_clip()))
    assert ops[0][0] == "clip" and ops[1][0] == "unclip"
    sb = _ops_after(lambda b: b.draw_scrollbar(0, 0, 10, 0.5, 0.3))
    assert sb[0][0] == "sbar" and sb[0][-1] == "vertical"


def test_panel_render_sends_a_frame():
    b = _backend()

    class FakeServer:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)
            return True

    b._server = FakeServer()
    panel = Panel(b)
    panel.add(Label("Hi"), x=0, y=0, w=10, h=1)
    panel.add(Button("OK"), x=0, y=2, w=8, h=1)
    panel.render()
    frame = json.loads(b._server.sent[-1])
    assert frame["type"] == "frame"
    assert isinstance(frame["ops"], list) and frame["ops"]


def _drain_queue(b):
    import queue as _q
    out = []
    while True:
        try:
            out.append(b._queue.get_nowait())
        except _q.Empty:
            return out


def test_ime_preedit_and_commit_events():
    from puikit.backends.web_backend import _TICK  # noqa: F401 (ensure import path)

    b = _backend()
    b._on_message('{"type":"ime_preedit","text":"にほん","caret":3}')
    b._on_message('{"type":"ime_commit","text":"日本"}')
    events = _drain_queue(b)
    # preedit event, a clearing preedit, then one KEY per committed character.
    assert events[0].type is EventType.IME_COMPOSITION
    assert events[0].hints["preedit"] == "にほん"
    assert events[1].type is EventType.IME_COMPOSITION and events[1].hints["preedit"] == ""
    assert [e.char for e in events[2:]] == ["日", "本"]
    assert all(e.type is EventType.KEY for e in events[2:])


def test_resize_fires_on_any_pixel_change():
    b = WebBackend(open_browser=False)
    b._on_message('{"type":"resize","w":800,"h":600}')  # first = connect, no event
    assert _drain_queue(b) == []
    b._on_message('{"type":"resize","w":801,"h":600}')  # sub-cell change still repaints
    events = _drain_queue(b)
    assert len(events) == 1 and events[0].type is EventType.RESIZE


def test_drain_coalesces_ticks():
    from puikit.backends.web_backend import _TICK
    from puikit.event import Event

    b = WebBackend(open_browser=False)
    ran = {"ticks": 0, "events": 0}
    b.request_animation_ticks(lambda: (ran.__setitem__("ticks", ran["ticks"] + 1) or True))
    for _ in range(5):
        b._queue.put(_TICK)
    b._queue.put(Event(EventType.KEY, key="a"))
    for _ in range(5):
        b._queue.put(_TICK)
    first = b._queue.get_nowait()
    b._drain(first, lambda e: ran.__setitem__("events", ran["events"] + 1))
    b._stop_ticker()
    # Ten queued ticks collapse to a single re-render; the event is still handled.
    assert ran["ticks"] == 1 and ran["events"] == 1


def test_css_color_rgb_and_rgba():
    assert _css_color((1, 2, 3)) == "rgba(1,2,3,1.000)"
    assert _css_color((1, 2, 3, 128)).endswith("0.502)")
    assert _css_color(None) is None


# --- transport round-trip (real socket, hand-rolled WS client) --------------

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_connect(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(b"0123456789abcdef").decode()
    s.sendall(
        f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(1024)
    assert b"101" in resp.split(b"\r\n")[0]
    expect = base64.b64encode(hashlib.sha1((key + _WS_MAGIC).encode()).digest()).decode()
    assert expect.encode() in resp
    return s


def _ws_send_text(s, text):
    payload = text.encode()
    mask = b"\x1a\x2b\x3c\x4d"
    masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    header = bytearray([0x81, 0x80 | len(payload)])  # FIN+text, MASK set, short len
    s.sendall(bytes(header) + mask + masked)


def _ws_recv_text(s):
    b1 = s.recv(1)
    b2 = s.recv(1)[0]
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", s.recv(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", s.recv(8))[0]
    data = b""
    while len(data) < length:
        data += s.recv(length - len(data))
    return data.decode()


def test_transport_handshake_and_roundtrip():
    received = []
    connected = []
    server = WebServer(
        asset_dir="/nonexistent",
        font_dir="/nonexistent",
        on_message=received.append,
        on_connect=lambda: connected.append(True),
    )
    port = server.start()
    try:
        s = _ws_connect(port)
        # Give the server thread a moment to register the connection.
        for _ in range(50):
            if connected:
                break
            time.sleep(0.02)
        assert connected

        # client -> server (masked)
        _ws_send_text(s, json.dumps({"type": "resize", "w": 640, "h": 480}))
        for _ in range(50):
            if received:
                break
            time.sleep(0.02)
        assert received and json.loads(received[0])["w"] == 640

        # server -> client (unmasked)
        assert server.send(json.dumps({"type": "frame", "ops": []}))
        msg = json.loads(_ws_recv_text(s))
        assert msg["type"] == "frame"
        s.close()
    finally:
        server.close()
