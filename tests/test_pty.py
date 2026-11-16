"""puikit._pty: a child on a pseudo-terminal (forkpty on POSIX, ConPTY on Windows)."""

import os
import sys
import threading

import pytest

from puikit import _pty

pytestmark = pytest.mark.skipif(not _pty.SUPPORTED, reason=str(_pty.UNSUPPORTED_REASON))

WINDOWS = sys.platform == "win32"


def _run(argv, *, stdin: bytes = b"", timeout: float = 15.0):
    out: list[bytes] = []
    done = threading.Event()
    result = {}

    def on_exit(code):
        result["code"] = code
        done.set()

    proc = _pty.PtyProcess(argv, cols=80, rows=24, cwd=os.path.expanduser("~"),
                           on_output=out.append, on_exit=on_exit)
    if stdin:
        proc.write(stdin)
    assert done.wait(timeout), "the child did not exit"
    return b"".join(out).decode("utf-8", "replace"), result["code"]


def test_output_and_exit_code():
    if WINDOWS:
        argv = ["cmd.exe", "/d", "/c", "echo pty-ok-ä& exit /b 3"]
    else:
        argv = ["/bin/sh", "-c", "printf 'pty-ok-\\303\\244\\n'; exit 3"]
    text, code = _run(argv)
    assert "pty-ok-ä" in text  # UTF-8 whatever the console code page
    assert code == 3


def test_input_reaches_the_child():
    if WINDOWS:
        argv, line = ["cmd.exe", "/d", "/q"], b"echo typed-%OS%& exit 0\r"
    else:
        argv, line = ["/bin/sh"], b"echo typed-$((40+2)); exit 0\r"
    text, code = _run(argv, stdin=line)
    assert ("typed-Windows_NT" if WINDOWS else "typed-42") in text
    assert code == 0


def test_close_hangs_up_a_running_child():
    done = threading.Event()
    argv = ["cmd.exe", "/d", "/q"] if WINDOWS else ["/bin/sh"]
    proc = _pty.PtyProcess(argv, cols=80, rows=24, on_output=lambda b: None,
                           on_exit=lambda code: done.set())
    proc.resize(100, 30)
    proc.close()
    assert done.wait(15)


def test_the_host_terminal_s_variables_stay_out(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "1234")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    env = _pty._child_env(None)
    assert "WT_SESSION" not in env and "TERM_PROGRAM" not in env
    assert env["PATH"] == os.environ["PATH"]
    env = _pty._child_env({"WT_SESSION": "x", "FOO": "bar"})
    assert env.get("FOO") == "bar" and "WT_SESSION" not in env


@pytest.mark.parametrize("split", range(0, 16))
def test_prompt_start_loses_its_fresh_line_wherever_the_read_splits(split):
    """ConPTY's 133;A, after conhost left the cursor in the corner, would
    scroll the screen a row per prompt; 133;P marks the same without."""
    data = b"\x1b[120C\x1b]133;A\x07~ 23:40> \x1b]133;B\x07"
    fix = _pty._PromptMarkFix()
    out = fix.feed(data[:split]) + fix.feed(data[split:]) + fix.flush()
    assert out == b"\x1b[120C\x1b]133;P\x07~ 23:40> \x1b]133;B\x07"


def test_prompt_mark_fix_leaves_everything_else_alone():
    fix = _pty._PromptMarkFix()
    for data in (b"plain text\r\n", b"\x1b]133;AB\x07", b"\x1b]0;title\x07",
                 b"\x1b]133;A;cl=m\x07"[:-1]):
        assert fix.feed(data) + fix.flush() == data.replace(b"133;A;", b"133;P;")
    assert fix.feed(b"\x1b]133;A;cl=m\x07") == b"\x1b]133;P;cl=m\x07"
    assert fix.feed(b"end\x1b") + fix.feed(b"[m") == b"end\x1b[m"


@pytest.mark.skipif(not WINDOWS, reason="POSIX reports a missing program as exit 127")
def test_missing_program_raises_on_windows():
    with pytest.raises(FileNotFoundError):
        _pty.PtyProcess(["no-such-program-xyz"], cols=80, rows=24,
                        on_output=lambda b: None, on_exit=lambda c: None)
