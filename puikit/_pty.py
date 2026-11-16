"""A child process on a pseudo-terminal (prototype, discussion xefm#900).

POSIX only for now: ``os.forkpty`` gives the child a new session with the pty
as its controlling terminal, so job control, ``SIGWINCH`` and full-screen
programs behave as in a real terminal. Windows needs ConPTY
(``CreatePseudoConsole``) behind the same small interface; not written yet.

Output is read on a daemon thread and handed to ``on_output`` there; the
caller moves it to its own thread (``TerminalView`` posts to the UI thread).
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import struct
import sys
import threading
import warnings
from collections.abc import Callable

log = logging.getLogger(__name__)

SUPPORTED = sys.platform != "win32"

# POSIX-only modules, imported only where they exist: the module must still
# import on Windows, so that TerminalView's ``available()`` can say why it
# cannot run there instead of the import failing first.
if SUPPORTED:
    import fcntl
    import termios


class PtyProcess:
    def __init__(self, argv: list[str], *, cols: int, rows: int,
                 cwd: str | None = None, env: dict[str, str] | None = None,
                 on_output: Callable[[bytes], None],
                 on_exit: Callable[[int | None], None]) -> None:
        if not SUPPORTED:
            raise OSError("PtyProcess: ConPTY is not implemented yet")
        self._on_output = on_output
        self._on_exit = on_exit
        env = dict(os.environ if env is None else env)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        # A GUI app launched from Finder has no locale; a shell without one
        # reads UTF-8 bytes as Meta-keys (readline), as Terminal.app avoids by
        # setting LANG. Same here, unless the caller chose one.
        if not any(env.get(k) for k in ("LC_ALL", "LC_CTYPE", "LANG")):
            env["LANG"] = "en_US.UTF-8"
        # fork in a threaded process is what pty/pexpect do too; the child execs
        # at once. (A spawn-based launcher is the proper fix — see the report.)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, fd = os.forkpty()
        if pid == 0:  # child
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        self.pid = pid
        self.fd = fd
        self.returncode: int | None = None
        self.resize(cols, rows)
        self._thread = threading.Thread(target=self._reader, name="pty-reader", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                break  # EIO: the child closed its side
            if not data:
                break
            self._on_output(data)
        try:
            _, status = os.waitpid(self.pid, 0)
            self.returncode = os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            pass
        self._on_exit(self.returncode)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
            except BlockingIOError:
                continue
            except OSError as exc:
                log.warning("pty write failed: %s", exc)
                return
            view = view[n:]

    def resize(self, cols: int, rows: int, px_w: int = 0, px_h: int = 0) -> None:
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, px_w, px_h))
        except OSError as exc:
            log.warning("pty resize failed: %s", exc)

    def close(self) -> None:
        if self.returncode is None:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass
