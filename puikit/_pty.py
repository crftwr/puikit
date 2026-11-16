"""A child process on a pseudo-terminal (discussion xefm#900).

POSIX: ``os.forkpty`` gives the child a new session with the pty as its
controlling terminal, so job control, ``SIGWINCH`` and full-screen programs
behave as in a real terminal.

Windows: a ConPTY (``CreatePseudoConsole``, Windows 10 1809 and later) is the
child's console. Conhost behind it turns the console API calls of cmd.exe,
PowerShell and every other console program into VT output, and the VT input
written to it into key events, so the caller sees the same byte streams as on
POSIX.

Output is read on a daemon thread and handed to ``on_output`` there; the
caller moves it to its own thread (``TerminalView`` posts to the UI thread).
``on_exit`` is called on that thread too, after the last output.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Callable

log = logging.getLogger(__name__)

WINDOWS = sys.platform == "win32"

# Platform modules, imported only where they exist: the module must import
# everywhere, so that TerminalView's ``available()`` can say why it cannot run
# instead of the import failing first.
if WINDOWS:
    import ctypes
    from ctypes import wintypes
else:
    import fcntl
    import signal
    import termios


def _conpty_support() -> str | None:
    """None when ConPTY is there, else why not (only called on Windows)."""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreatePseudoConsole  # noqa: B018 - the lookup is the check
    except (OSError, AttributeError):
        return "ConPTY needs Windows 10 version 1809 or later"
    return None


#: None when a pty can be made here, else why not.
UNSUPPORTED_REASON: str | None = _conpty_support() if WINDOWS else None
SUPPORTED = UNSUPPORTED_REASON is None


#: Variables by which a program recognises the terminal the *host* app was
#: started from. Inherited, they would be false in here: a shell that sees
#: ``WT_SESSION`` writes Windows Terminal's escapes, one that sees
#: ``TERM_PROGRAM=iTerm.app`` iTerm2's.
_HOST_TERMINAL_VARS = (
    "WT_SESSION", "WT_PROFILE_ID",                       # Windows Terminal
    "TERM_PROGRAM", "TERM_PROGRAM_VERSION",              # iTerm2, Terminal.app, VS Code, ...
    "ITERM_SESSION_ID", "ITERM_PROFILE", "LC_TERMINAL", "LC_TERMINAL_VERSION",
    "KITTY_WINDOW_ID", "KITTY_PID", "KITTY_PUBLIC_KEY",
    "WEZTERM_PANE", "WEZTERM_EXECUTABLE", "WEZTERM_EXECUTABLE_DIR", "WEZTERM_UNIX_SOCKET",
    "GHOSTTY_RESOURCES_DIR", "GHOSTTY_BIN_DIR", "GHOSTTY_SHELL_FEATURES",
    "VTE_VERSION", "KONSOLE_VERSION", "KONSOLE_DBUS_SESSION", "KONSOLE_DBUS_WINDOW",
    "ALACRITTY_WINDOW_ID", "ALACRITTY_SOCKET",
)


def _child_env(env: dict[str, str] | None) -> dict[str, str]:
    env = dict(os.environ if env is None else env)
    for name in _HOST_TERMINAL_VARS:
        env.pop(name, None)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    # A GUI app launched from Finder has no locale; a shell without one reads
    # UTF-8 bytes as Meta-keys (readline), as Terminal.app avoids by setting
    # LANG. Same here, unless the caller chose one. (On Windows it is read only
    # by MSYS programs - Git's less and bash - which want UTF-8 too: ConPTY's
    # output is UTF-8 whatever the console code page.)
    if not any(env.get(k) for k in ("LC_ALL", "LC_CTYPE", "LANG")):
        env["LANG"] = "en_US.UTF-8"
    return env


class _PosixPty:
    def __init__(self, argv: list[str], *, cols: int, rows: int,
                 cwd: str | None, env: dict[str, str],
                 on_output: Callable[[bytes], None],
                 on_exit: Callable[[int | None], None]) -> None:
        self._on_output = on_output
        self._on_exit = on_exit
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


# --- Windows: ConPTY ------------------------------------------------------------
if WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    HPCON = wintypes.HANDLE

    class _COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    _S_OK = 0
    _INFINITE = 0xFFFFFFFF
    _STILL_ACTIVE = 259
    _WAIT_TIMEOUT = 0x102
    _STARTF_USESTDHANDLES = 0x00000100
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016

    def _proto(name, restype, *argtypes):
        fn = getattr(_k32, name, None)
        if fn is not None:
            fn.restype = restype
            fn.argtypes = argtypes
        return fn

    _CreatePipe = _proto("CreatePipe", wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE),
                         ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD)
    _CloseHandle = _proto("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _ReadFile = _proto("ReadFile", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p,
                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    _WriteFile = _proto("WriteFile", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p,
                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    _CreatePseudoConsole = _proto("CreatePseudoConsole", ctypes.c_long, _COORD,
                                  wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
                                  ctypes.POINTER(HPCON))
    _ResizePseudoConsole = _proto("ResizePseudoConsole", ctypes.c_long, HPCON, _COORD)
    _ClosePseudoConsole = _proto("ClosePseudoConsole", None, HPCON)
    _InitializeProcThreadAttributeList = _proto(
        "InitializeProcThreadAttributeList", wintypes.BOOL, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t))
    _UpdateProcThreadAttribute = _proto(
        "UpdateProcThreadAttribute", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
    _DeleteProcThreadAttributeList = _proto("DeleteProcThreadAttributeList", None,
                                            ctypes.c_void_p)
    _CreateProcessW = _proto(
        "CreateProcessW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION))
    _WaitForSingleObject = _proto("WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE,
                                  wintypes.DWORD)
    _TerminateProcess = _proto("TerminateProcess", wintypes.BOOL, wintypes.HANDLE,
                               wintypes.UINT)
    _GetExitCodeProcess = _proto("GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE,
                                 ctypes.POINTER(wintypes.DWORD))

    def _winerr(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(errno.EINVAL, f"{what}: {ctypes.FormatError(code).strip()}",
                       None, code)

    def _coord(cols: int, rows: int) -> _COORD:
        return _COORD(max(1, min(cols, 32767)), max(1, min(rows, 32767)))

    def _env_block(env: dict[str, str]) -> ctypes.Array:
        # Sorted case-insensitively, as CreateProcess documents; NUL-separated,
        # ending in an empty string.
        items = sorted(env.items(), key=lambda kv: kv[0].upper())
        text = "".join(f"{k}={v}\0" for k, v in items if k and "=" not in k[1:]) + "\0"
        return ctypes.create_unicode_buffer(text, len(text))

    def _resolve(argv: list[str], env: dict[str, str]) -> str:
        """The program as execvpe would find it: CreateProcess searches PATH
        but appends only ``.exe``, never the rest of PATHEXT (``.cmd``)."""
        prog = argv[0]
        found = shutil.which(prog, path=env.get("PATH") or env.get("Path"))
        if found is None:
            raise FileNotFoundError(errno.ENOENT, "program not found", prog)
        return found


class _PromptMarkFix:
    """Turns ConPTY's ``OSC 133;A`` (prompt start) into ``OSC 133;P``.

    Both mark where a prompt starts; A also starts a fresh line when the
    cursor is not in the first column, as the shell writing it means: "my
    prompt goes on a line of its own". Through ConPTY that cursor is not the
    shell's. Conhost draws the screen itself and passes the mark through out
    of step with its drawing - after a repaint has left the cursor in the
    bottom-right corner, from where the fresh line scrolls the whole screen up
    one row, and does so again on every prompt (eosh, oh-my-posh). P is the
    same mark without the fresh line; conhost's own cursor moves place the
    prompt. Sequences split across reads are held back until complete."""

    _MARK = b"\x1b]133;A"
    _ENDS = (b";", b"\x07", b"\x1b")

    def __init__(self) -> None:
        self._held = b""

    def feed(self, data: bytes) -> bytes:
        data = self._held + data
        self._held = b""
        if b"\x1b]" not in data and not data.endswith(b"\x1b"):
            return data
        out = bytearray()
        i = 0
        mark = self._MARK
        while True:
            j = data.find(mark, i)
            if j < 0:
                break
            end = j + len(mark)
            if end == len(data):  # its terminator has not arrived yet
                out += data[i:j]
                self._held = data[j:]
                return bytes(out)
            out += data[i:j]
            out += b"\x1b]133;P" if data[end:end + 1] in self._ENDS else mark
            i = end
        # A partial mark at the very end waits for the next read.
        tail = data[i:]
        for k in range(min(len(mark) - 1, len(tail)), 0, -1):
            if mark.startswith(tail[-k:]):
                out += tail[:-k]
                self._held = tail[-k:]
                return bytes(out)
        out += tail
        return bytes(out)

    def flush(self) -> bytes:
        held, self._held = self._held, b""
        return held


class _ConPty:
    #: How long a hung-up child gets to exit before it is terminated: a console
    #: program may clean up on CTRL_CLOSE_EVENT, and one still starting up when
    #: the console closed may never receive it.
    HANGUP_GRACE_SECONDS = 2.0

    def __init__(self, argv: list[str], *, cols: int, rows: int,
                 cwd: str | None, env: dict[str, str],
                 on_output: Callable[[bytes], None],
                 on_exit: Callable[[int | None], None]) -> None:
        self._on_output = on_output
        self._on_exit = on_exit
        self.returncode: int | None = None
        self._lock = threading.Lock()
        self._hpc = None
        self._in_w = self._out_r = None
        self._hproc = None
        self._hung_up_at: float | None = None
        program = _resolve(argv, env)
        cmdline = subprocess.list2cmdline([program] + list(argv[1:]))

        in_r, in_w = wintypes.HANDLE(), wintypes.HANDLE()
        out_r, out_w = wintypes.HANDLE(), wintypes.HANDLE()
        if not _CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), None, 0):
            raise _winerr("CreatePipe")
        if not _CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), None, 0):
            _CloseHandle(in_r)
            _CloseHandle(in_w)
            raise _winerr("CreatePipe")
        self._in_w, self._out_r = in_w, out_r
        hpc = HPCON()
        hr = _CreatePseudoConsole(_coord(cols, rows), in_r, out_w, 0, ctypes.byref(hpc))
        # Conhost holds its own copies of its ends of the pipes.
        _CloseHandle(in_r)
        _CloseHandle(out_w)
        if hr != _S_OK:
            self._close_handles()
            raise OSError(errno.EINVAL, f"CreatePseudoConsole failed (0x{hr & 0xFFFFFFFF:08x})")
        self._hpc = hpc

        try:
            self._spawn(program, cmdline, cwd, env)
        except BaseException:
            _ClosePseudoConsole(self._hpc)
            self._hpc = None
            self._close_handles()
            raise
        self.pid = self._pid
        self._thread = threading.Thread(target=self._reader, name="pty-reader", daemon=True)
        self._thread.start()
        self._waiter = threading.Thread(target=self._wait, name="pty-waiter", daemon=True)
        self._waiter.start()

    def _spawn(self, program: str, cmdline: str, cwd: str | None,
               env: dict[str, str]) -> None:
        size = ctypes.c_size_t()
        _InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not _InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)):
            raise _winerr("InitializeProcThreadAttributeList")
        try:
            # The attribute's value is the HPCON itself, passed by value.
            if not _UpdateProcThreadAttribute(attrs, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                              self._hpc, ctypes.sizeof(HPCON), None, None):
                raise _winerr("UpdateProcThreadAttribute")
            si = _STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
            # No std handles of ours: without this a child of a process whose
            # stdio is redirected inherits that redirection instead of the
            # pseudo console's handles.
            si.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
            pi = _PROCESS_INFORMATION()
            cmd_buf = ctypes.create_unicode_buffer(cmdline)
            env_buf = _env_block(env)
            if not _CreateProcessW(program, cmd_buf, None, None, False,
                                   _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
                                   env_buf, cwd or None, ctypes.byref(si), ctypes.byref(pi)):
                err = ctypes.get_last_error()
                if err in (2, 3):  # file / path not found
                    raise FileNotFoundError(errno.ENOENT, ctypes.FormatError(err).strip(),
                                            cwd if err == 3 else program)
                raise _winerr("CreateProcess")
        finally:
            _DeleteProcThreadAttributeList(attrs)
        _CloseHandle(pi.hThread)
        self._hproc = pi.hProcess
        self._pid = pi.dwProcessId

    def _wait(self) -> None:
        """Close the pseudo console once the child exits: conhost then ends,
        and the output pipe breaks after its last bytes, ending the reader.
        (Conhost keeps the pipe open as long as the console exists.) A child
        still there ``HANGUP_GRACE_SECONDS`` after :meth:`close` is terminated.
        This thread owns the process handle until it returns."""
        while _WaitForSingleObject(self._hproc, 100) == _WAIT_TIMEOUT:
            hung_up = self._hung_up_at
            if hung_up is not None and time.monotonic() - hung_up > self.HANGUP_GRACE_SECONDS:
                _TerminateProcess(self._hproc, 1)
                _WaitForSingleObject(self._hproc, _INFINITE)
                break
        code = wintypes.DWORD()
        if _GetExitCodeProcess(self._hproc, ctypes.byref(code)) \
                and code.value != _STILL_ACTIVE:
            # An NTSTATUS such as 0xC000013A (Ctrl-C) reads as negative, as
            # subprocess reports it.
            self.returncode = ctypes.c_int32(code.value).value
        self._close_console()

    def _reader(self) -> None:
        buf = ctypes.create_string_buffer(65536)
        n = wintypes.DWORD()
        fix = _PromptMarkFix()
        while True:
            if not _ReadFile(self._out_r, buf, len(buf), ctypes.byref(n), None) \
                    or n.value == 0:
                break  # broken pipe: the console is gone
            data = fix.feed(buf.raw[:n.value])
            if data:
                self._on_output(data)
        rest = fix.flush()
        if rest:
            self._on_output(rest)
        self._waiter.join()
        with self._lock:
            if self._hproc is not None:
                _CloseHandle(self._hproc)
                self._hproc = None
            if self._out_r is not None:
                _CloseHandle(self._out_r)
                self._out_r = None
        self._on_exit(self.returncode)

    def _close_console(self) -> None:
        with self._lock:
            hpc, self._hpc = self._hpc, None
        if hpc is not None:
            # Sends CTRL_CLOSE_EVENT to whatever is still attached, as closing
            # a console window does. The reader is still draining the output
            # pipe, which older conhosts need before this returns.
            _ClosePseudoConsole(hpc)

    def _close_handles(self) -> None:
        for name in ("_in_w", "_out_r"):
            h = getattr(self, name)
            if h is not None:
                _CloseHandle(h)
                setattr(self, name, None)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        n = wintypes.DWORD()
        while view:
            with self._lock:
                h = self._in_w
            if h is None:
                return
            chunk = bytes(view[:65536])
            if not _WriteFile(h, chunk, len(chunk), ctypes.byref(n), None):
                log.warning("pty write failed: %s", ctypes.FormatError(ctypes.get_last_error()))
                return
            view = view[n.value:]

    def resize(self, cols: int, rows: int, px_w: int = 0, px_h: int = 0) -> None:
        with self._lock:
            hpc = self._hpc
            if hpc is None:
                return
            hr = _ResizePseudoConsole(hpc, _coord(cols, rows))
        if hr != _S_OK:
            log.warning("pty resize failed (0x%08x)", hr & 0xFFFFFFFF)

    def close(self) -> None:
        # Closing the console hangs up the child (CTRL_CLOSE_EVENT), as SIGHUP
        # does on POSIX; the waiter and the reader then wind down by themselves.
        if self._hung_up_at is None:
            self._hung_up_at = time.monotonic()
        self._close_console()
        with self._lock:
            if self._in_w is not None:
                _CloseHandle(self._in_w)
                self._in_w = None


class PtyProcess:
    """``argv`` running on a new pseudo-terminal of ``cols`` x ``rows``.

    ``FileNotFoundError`` when the program (or ``cwd``) does not exist on
    Windows; on POSIX the child exits with 127 instead, as a shell's would."""

    def __init__(self, argv: list[str], *, cols: int, rows: int,
                 cwd: str | None = None, env: dict[str, str] | None = None,
                 on_output: Callable[[bytes], None],
                 on_exit: Callable[[int | None], None]) -> None:
        if not SUPPORTED:
            raise OSError(f"PtyProcess: {UNSUPPORTED_REASON}")
        impl = _ConPty if WINDOWS else _PosixPty
        self._impl = impl(argv, cols=cols, rows=rows, cwd=cwd, env=_child_env(env),
                          on_output=on_output, on_exit=on_exit)
        self.pid = self._impl.pid

    @property
    def returncode(self) -> int | None:
        return self._impl.returncode

    def write(self, data: bytes) -> None:
        self._impl.write(data)

    def resize(self, cols: int, rows: int, px_w: int = 0, px_h: int = 0) -> None:
        self._impl.resize(cols, rows, px_w, px_h)

    def close(self) -> None:
        self._impl.close()
