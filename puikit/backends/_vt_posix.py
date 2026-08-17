"""The VT backend's POSIX console half: a raw tty, select(), and TIOCGWINSZ.

``_WindowsConsole`` drives the Win32 console API; this drives a Unix tty with
termios and reads the same VT stream every xterm descendant writes. The engine
in ``vt_backend`` cannot tell them apart: both hand it the same input records
(``_vt_input`` does the byte-stream decoding here) and take each frame as one
write.

Design notes, where this deliberately differs from the obvious:

* **Resize is polled, not signalled.** ``read_input`` compares TIOCGWINSZ
  against the last known size on every wake. A SIGWINCH handler would surface
  the change no sooner — the flag it sets is only seen on the same wake — and
  polling costs one ioctl per 50ms tick while working from any thread and
  leaving the host process's signal handlers alone.
* **Ctrl+C/Z/S/Q are keys, not signals** (ISIG/IXON cleared): the same
  contract the Windows console gets by clearing ``ENABLE_PROCESSED_INPUT``,
  so an app binds Ctrl+C to copy on both.
* **An unfinished escape sequence waits briefly for its tail.** A terminal
  sends a sequence in one burst, so one short grace select separates "the rest
  is in flight" from "the user pressed ESC" — the same trade the curses
  backend makes with ESCDELAY=100.
"""

from __future__ import annotations

import base64
import codecs
import fcntl
import os
import re
import select
import struct
import sys
import termios
import time

from ._vt_input import parse_vt_input

#: DECSET modes: 1000 click, 1002 motion-while-held (no hover flood — hover is
#: off in the TUI profile), 1006 SGR encoding. The same modes the curses
#: backend has to drive by hand because macOS ncurses will not.
_MOUSE_ON = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
_MOUSE_OFF = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"
#: Alternate screen + hidden cursor, and the way back out (attributes reset so
#: nothing leaks into the shell).
_ENTER = "\x1b[?1049h\x1b[?25l"
_LEAVE = "\x1b[0m\x1b[?25h\x1b[?1049l"

#: How long to wait for the rest of a partially-arrived escape sequence.
_ESCAPE_GRACE_S = 0.1


class PosixConsole:
    """The tty half: termios modes, size, batched output, and parsed input."""

    def __init__(self, in_fd: "int | None" = None, out_fd: "int | None" = None) -> None:
        # sys.__stdin__/__stdout__, not sys.stdin/stdout: a host app that
        # redirected the streams (e.g. to a log pane) must not swallow the
        # frames — the same reasoning as the curses backend's _raw_out.
        self._in_fd = sys.__stdin__.fileno() if in_fd is None else in_fd
        self._out_fd = sys.__stdout__.fileno() if out_fd is None else out_fd
        self._saved_attrs: "list | None" = None
        self._last_size: "tuple[int, int] | None" = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._cell_px: "tuple[float, float] | None" = None
        # A closed stdin selects readable forever while reading nothing; once
        # seen, stop selecting on it so the event loop keeps its idle cadence
        # instead of spinning hot.
        self._eof = False

    # --- mode ---

    def open(self) -> None:
        # Remember the shell's settings ONLY on first entry: suspend/resume
        # also re-applies raw mode, and re-reading then would save our own raw
        # mode as the thing to restore on exit, leaving the user's shell in it
        # — the same trap the Windows console documents around its saved modes.
        if self._saved_attrs is None:
            try:
                self._saved_attrs = termios.tcgetattr(self._in_fd)
            except termios.error:
                self._saved_attrs = None
        self._apply_raw()
        self._last_size = self.size()
        self.write(_ENTER + _MOUSE_ON)

    def _apply_raw(self) -> None:
        """Raw input, untouched output: keys arrive unassembled and unechoed,
        Enter stays ``\\r`` (ICRNL off), and Ctrl+C/Z/S/Q reach the app as keys
        (ISIG/IXON off). Derived from the SAVED attributes rather than the
        current ones so a child process's leavings cannot bleed into our mode
        on resume. OPOST is left alone: frames are absolute-positioned VT with
        no bare newlines, so NL translation is moot either way."""
        if self._saved_attrs is None:
            return
        attrs = self._saved_attrs[:]
        attrs[6] = attrs[6][:]  # the cc list — don't mutate the saved copy's
        attrs[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK
                      | termios.ISTRIP | termios.IXON)
        attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN
                      | termios.ISIG)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        self._set_attrs(attrs)

    def _restore(self) -> None:
        if self._saved_attrs is not None:
            self._set_attrs(self._saved_attrs)

    def _set_attrs(self, attrs: list) -> None:
        # TCSANOW, not TCSADRAIN: none of the bits changed here affect how
        # pending OUTPUT is rendered (oflag is untouched), and DRAIN blocks
        # until the terminal consumes that output — a stalled terminal would
        # then wedge suspend() and exit, the two places restore runs.
        try:
            termios.tcsetattr(self._in_fd, termios.TCSANOW, attrs)
        except termios.error:
            pass

    def close(self) -> None:
        self.write(_MOUSE_OFF + _LEAVE)
        self._restore()

    def suspend(self) -> None:
        """Give the terminal back to a child process: stop mouse reports, leave
        the alternate screen, show the cursor, and put the tty back in the
        shell's modes — the child expects line input and echo, and would
        otherwise run with our raw, mouse-reporting mode still in force."""
        self.write(_MOUSE_OFF + _LEAVE)
        self._restore()

    def resume(self) -> None:
        """Take it back. Re-applying the modes is not enough on its own: the
        alternate screen returns blank, so the caller repaints."""
        self._apply_raw()
        self.write(_ENTER + _MOUSE_ON)

    # --- geometry ---

    def _winsz(self) -> tuple[int, int, int, int]:
        """rows, cols, xpixel, ypixel from TIOCGWINSZ. The pixel fields are 0
        where the terminal does not fill them in (Terminal.app, notably)."""
        try:
            data = fcntl.ioctl(self._out_fd, termios.TIOCGWINSZ, b"\x00" * 8)
            return struct.unpack("HHHH", data)
        except OSError:
            return (0, 0, 0, 0)

    def size(self) -> tuple[int, int]:
        rows, cols, _, _ = self._winsz()
        if rows and cols:
            return (cols, rows)
        return (80, 24)

    def cell_pixels(self) -> tuple[float, float]:
        """Pixel size of one character cell, for scaling inline images.

        TIOCGWINSZ answers without a terminal round trip on emulators that fill
        the pixel fields in (iTerm2, kitty, WezTerm — the ones with an image
        protocol to scale for). The XTWINOPS probe is the fallback, and 8x16
        the guess of last resort — by then no image protocol was detected
        anyway, so nothing is scaled by it.
        """
        if self._cell_px is not None:
            return self._cell_px
        self._cell_px = (self._ioctl_cell_pixels() or self._probe_cell_pixels()
                         or (8.0, 16.0))
        return self._cell_px

    def _ioctl_cell_pixels(self) -> "tuple[float, float] | None":
        rows, cols, xpix, ypix = self._winsz()
        if not (rows and cols and xpix and ypix):
            return None
        w, h = xpix / cols, ypix / rows
        if not (1.0 <= w <= 200.0 and 1.0 <= h <= 400.0):
            return None  # implausible answer; ask the terminal instead
        return (w, h)

    def _probe_cell_pixels(self) -> "tuple[float, float] | None":
        """Ask the terminal (``CSI 14 t`` / ``CSI 18 t``), as the Windows
        console does. Whatever else arrives while waiting stays in the input
        buffer for read_input; the replies themselves are cut out of it. A
        reply that only lands after the deadline needs no special filter here,
        unlike on Windows: parse_vt_input drops an unrecognized CSI instead of
        typing it into the app."""
        self.write("\x1b[14t\x1b[18t")
        deadline = time.monotonic() + 0.25
        pixels = cells = None
        while time.monotonic() < deadline and (pixels is None or cells is None):
            if not self._wait_readable(deadline - time.monotonic()):
                break
            data = self._read_bytes()
            if not data:
                break
            self._buffer += self._decoder.decode(data)
            for match in re.finditer(r"\x1b\[(4|8);(\d+);(\d+)t", self._buffer):
                kind, a, b = match.group(1), int(match.group(2)), int(match.group(3))
                if kind == "4":
                    pixels = (b, a)   # width, height
                else:
                    cells = (b, a)    # cols, rows
        self._buffer = re.sub(r"\x1b\[(?:4|8);\d+;\d+t", "", self._buffer)
        if not pixels or not cells or not all(pixels) or not all(cells):
            return None
        w = pixels[0] / cells[0]
        h = pixels[1] / cells[1]
        if not (1.0 <= w <= 200.0 and 1.0 <= h <= 400.0):
            return None
        return (w, h)

    # --- output ---

    def write(self, data: str) -> None:
        """One frame, one buffer handed to the tty (os.write may still split
        it; the loop finishes the job). No Python-level stream, so no interplay
        with whatever the host did to sys.stdout."""
        if not data:
            return
        payload = data.encode("utf-8", "replace")
        while payload:
            try:
                written = os.write(self._out_fd, payload)
            except OSError:
                return
            payload = payload[written:]

    # --- input ---

    def read_input(self, timeout_ms: int) -> list[dict]:
        records: list[dict] = []
        size = self.size()
        if self._last_size is not None and size != self._last_size:
            records.append({"type": "resize"})
        self._last_size = size
        if self._buffer:
            # Bytes already read but not yet parsed — what the cell-pixel
            # probe drained off the fd besides its replies.
            parsed, self._buffer = parse_vt_input(self._buffer)
            records.extend(parsed)
        # A resize or held-over input should not also sit out the timeout:
        # deliver it now, with whatever more is already pending.
        timeout = 0.0 if records else max(0, int(timeout_ms)) / 1000.0
        if self._eof:
            time.sleep(timeout)
            return records
        if not self._wait_readable(timeout):
            return records
        data = self._read_bytes()
        if not data:
            self._eof = True
            return records
        self._buffer += self._decoder.decode(data)
        parsed, self._buffer = parse_vt_input(self._buffer)
        records.extend(parsed)
        if self._buffer:
            records.extend(self._finish_escape())
        return records

    def _finish_escape(self) -> list[dict]:
        """The buffer ends mid-escape-sequence. Wait briefly for the rest —
        a continuation that is coming arrives immediately, since the terminal
        sends a sequence in one burst — then force the decision: the ESC was
        the Escape key."""
        deadline = time.monotonic() + _ESCAPE_GRACE_S
        out: list[dict] = []
        while self._buffer:
            if not self._wait_readable(deadline - time.monotonic()):
                parsed, self._buffer = parse_vt_input(self._buffer, flush=True)
                out.extend(parsed)
                break
            data = self._read_bytes()
            if not data:
                self._eof = True
                parsed, self._buffer = parse_vt_input(self._buffer, flush=True)
                out.extend(parsed)
                break
            self._buffer += self._decoder.decode(data)
            parsed, self._buffer = parse_vt_input(self._buffer)
            out.extend(parsed)
        return out

    def _wait_readable(self, timeout_s: float) -> bool:
        try:
            ready, _, _ = select.select([self._in_fd], [], [], max(0.0, timeout_s))
        except OSError:
            return False
        return bool(ready)

    def _read_bytes(self) -> bytes:
        try:
            return os.read(self._in_fd, 65536)
        except OSError:
            return b""

    # --- clipboard ---

    def set_clipboard(self, text: str) -> None:
        """Copy via OSC 52, which rides the output stream and so reaches the
        *user's* clipboard even over SSH — the local terminal decodes it. It is
        write-only; the engine's process-local buffer covers paste."""
        try:
            payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        except ValueError:
            return
        seq = f"\x1b]52;c;{payload}\x07"
        # Inside tmux the sequence must be wrapped in a passthrough envelope
        # (with its own ESCs doubled) or tmux swallows it instead of relaying
        # it to the outer terminal.
        if os.environ.get("TMUX"):
            seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
        self.write(seq)

    def get_clipboard(self) -> str:
        return ""  # OSC 52 cannot read back; the engine falls back to its buffer
