"""Local HTTP + WebSocket server for the web backend.

The web backend talks to a browser tab over one WebSocket connection: the
Python side pushes a serialized display list per frame, and the browser pushes
back input events. This module owns the transport and nothing else — it knows
how to serve the client page, upgrade a request to a WebSocket, frame text
messages both ways, and hand decoded client messages to a callback. It has no
idea what a "frame" or an "event" is; that lives in ``web_backend``.

It is hand-rolled (``socket`` + ``http.server`` + ``hashlib``) to keep PuiKit
dependency-free, the same choice the Windows backend makes with raw ctypes. Only
the RFC 6455 subset a single trusted localhost client needs is implemented:
unfragmented text frames, ping/pong, and close. There is exactly one client (the
launched browser tab); a later connection replaces an earlier one.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# RFC 6455 handshake magic: appended to the client key before SHA-1 so both ends
# prove they speak WebSocket rather than echoing an arbitrary HTTP response.
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

_OP_TEXT = 0x1
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".ttf": "font/ttf",
}


class WebServer:
    """Serves the client page and bridges one WebSocket to Python callbacks."""

    def __init__(
        self,
        asset_dir: str,
        font_dir: str,
        on_message: Callable[[str], None],
        on_connect: Callable[[], None],
    ):
        self._asset_dir = asset_dir
        self._font_dir = font_dir
        self._on_message = on_message
        self._on_connect = on_connect
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # The single live client socket and a lock guarding writes to it (the UI
        # thread sends frames while the reader thread may be closing it).
        self._conn = None
        self._send_lock = threading.Lock()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> int:
        """Bind an ephemeral localhost port, serve in a daemon thread, return it."""
        server = self  # closure handle for the handler

        class Handler(BaseHTTPRequestHandler):
            # Silence the default per-request stderr logging.
            def log_message(self, *args):  # noqa: D401 - stdlib signature
                pass

            def do_GET(self):
                if self.path.split("?")[0] == "/ws":
                    server._handle_ws(self)
                    return
                server._serve_asset(self)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return httpd.server_address[1]

    def close(self) -> None:
        conn = self._conn
        if conn is not None:
            try:
                self._send_frame(conn, _OP_CLOSE, b"")
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            self._conn = None
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # --- static assets -----------------------------------------------------

    def _serve_asset(self, handler: BaseHTTPRequestHandler) -> None:
        path = handler.path.split("?")[0]
        if path == "/":
            path = "/index.html"
        if path.startswith("/fonts/"):
            root, rel = self._font_dir, path[len("/fonts/") :]
        else:
            root, rel = self._asset_dir, path.lstrip("/")
        # Contain the served file to the asset/font roots — a client cannot walk
        # out with "..", even though only our own launched page requests here.
        full = os.path.normpath(os.path.join(root, rel))
        if not full.startswith(os.path.normpath(root) + os.sep):
            handler.send_error(403)
            return
        if not os.path.isfile(full):
            handler.send_error(404)
            return
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = _CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(body)

    # --- websocket ---------------------------------------------------------

    def _handle_ws(self, handler: BaseHTTPRequestHandler) -> None:
        key = handler.headers.get("Sec-WebSocket-Key")
        if not key:
            handler.send_error(400)
            return
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_MAGIC).encode()).digest()
        ).decode()
        handler.wfile.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
        )
        handler.wfile.flush()

        conn = handler.connection
        self._conn = conn
        self._on_connect()
        # This request's thread becomes the reader loop for the connection's life.
        # All reads go through the handler's buffered rfile so no bytes are lost
        # to a raw-recv / buffer split; writes go straight to the socket.
        try:
            self._read_loop(handler)
        except OSError:
            pass
        finally:
            if self._conn is conn:
                self._conn = None

    def _read_loop(self, handler: BaseHTTPRequestHandler) -> None:
        rfile = handler.rfile
        conn = handler.connection
        while True:
            header = rfile.read(2)
            if len(header) < 2:
                return
            b1, b2 = header[0], header[1]
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", rfile.read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", rfile.read(8))[0]
            mask = rfile.read(4) if masked else b""
            payload = rfile.read(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))

            if opcode == _OP_TEXT:
                try:
                    self._on_message(payload.decode("utf-8"))
                except Exception:  # noqa: BLE001 - a bad client message must not kill the reader
                    pass
            elif opcode == _OP_CLOSE:
                return
            elif opcode == _OP_PING:
                self._send_frame(conn, _OP_PONG, payload)
            # _OP_PONG and continuation frames are ignored (our client sends
            # small unfragmented text messages only).

    def send(self, text: str) -> bool:
        """Send one text frame to the client. Returns False if not connected."""
        conn = self._conn
        if conn is None:
            return False
        try:
            self._send_frame(conn, _OP_TEXT, text.encode("utf-8"))
            return True
        except OSError:
            self._conn = None
            return False

    def _send_frame(self, conn, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])  # FIN set, server frames are unmasked
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        with self._send_lock:
            conn.sendall(bytes(header) + payload)
