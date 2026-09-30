"""gh #151: a `shutdown` command must end the raw stdio loop with exit 0.

The background reader thread (``_CommandIntake``) is a daemon. After `shutdown` it is
still blocked waiting for the next line, because the extension host keeps the pipe open
until the child exits. It used to wait inside ``sys.stdin.buffer`` (a ``BufferedReader``)
holding that object's lock, so finalizing the interpreter aborted with ``Fatal Python
error: _enter_buffered_busy ... possibly due to daemon threads``: SIGABRT (134) on Linux,
an access violation on Windows. Keeping stdin open made it happen on every run. The
reader now reads the unbuffered ``sys.stdin.buffer.raw``, which has no such lock.

These drive the real ``python -m langstage_vscode --demo`` the way the extension does:
write the commands, keep stdin open, wait for the exit.
"""

import io
import json
import os
import subprocess
import sys
import threading
from functools import partial
from pathlib import Path

import pytest

from langstage_vscode.sidecar import _raw_lines, _stdin_lines

_SHUTDOWN = b'{"type": "shutdown"}\n'
_TIMEOUT = 120


def _env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT"):
        env.pop(var, None)
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    return env


def _spawn(tmp_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "langstage_vscode", "--demo"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(tmp_path), env=_env(tmp_path),
    )


def _drain(stream, sink: list) -> threading.Thread:
    t = threading.Thread(target=lambda: sink.append(stream.read()), daemon=True)
    t.start()
    return t


def _run_holding_stdin_open(tmp_path: Path, commands: bytes) -> tuple[int, list[dict], str]:
    """Send ``commands`` after `ready`, keep stdin open, and wait for the process to
    exit on its own. Returns (exit code, stdout frames, stderr)."""
    proc = _spawn(tmp_path)
    try:
        first = proc.stdout.readline()  # `ready`: the reader thread is now blocked on stdin
        assert json.loads(first)["type"] == "ready", first
        out: list = []
        err: list = []
        readers = [_drain(proc.stdout, out), _drain(proc.stderr, err)]
        proc.stdin.write(commands)
        proc.stdin.flush()
        # Do NOT close stdin: the extension keeps the pipe open and waits for the exit.
        rc = proc.wait(timeout=_TIMEOUT)
        for t in readers:
            t.join(timeout=_TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdin.close()
    frames = [json.loads(ln) for ln in (first + out[0]).decode("utf-8").splitlines() if ln.strip()]
    return rc, frames, err[0].decode("utf-8", "replace")


def test_shutdown_with_stdin_held_open_exits_0(tmp_path):
    rc, frames, stderr = _run_holding_stdin_open(tmp_path, _SHUTDOWN)
    assert "Fatal Python error" not in stderr, stderr
    assert rc == 0, stderr
    assert [f["type"] for f in frames] == ["ready"]


def test_shutdown_after_a_turn_exits_0_with_every_frame_flushed(tmp_path):
    turn = json.dumps({"type": "message", "session_id": "s1", "content": "hello"}).encode()
    rc, frames, stderr = _run_holding_stdin_open(tmp_path, turn + b"\n" + _SHUTDOWN)
    assert "Fatal Python error" not in stderr, stderr
    assert rc == 0, stderr
    types = [f["type"] for f in frames]
    assert types[0] == "ready" and types[-1] == "turn_end", types
    assert "complete" in types and "content" in types, types


def test_eof_still_exits_0(tmp_path):
    """The EOF path (stdin closed, no `shutdown`) already exited cleanly; it still does."""
    proc = _spawn(tmp_path)
    out, err = proc.communicate(
        json.dumps({"type": "message", "session_id": "s1", "content": "hi"}).encode() + b"\n",
        timeout=_TIMEOUT,
    )
    stderr = err.decode("utf-8", "replace")
    assert "Fatal Python error" not in stderr, stderr
    assert proc.returncode == 0, stderr
    assert out.decode("utf-8").strip().splitlines()[-1] == '{"type": "turn_end", "session_id": "s1"}'


# ---- the unbuffered reader --------------------------------------------------------


def _pipe_lines(chunks: list[bytes], chunk_size: int = 65536) -> list[bytes]:
    r, w = os.pipe()

    def feed() -> None:
        for c in chunks:
            os.write(w, c)
        os.close(w)

    t = threading.Thread(target=feed)
    t.start()
    try:
        return list(_raw_lines(partial(os.read, r), chunk_size))
    finally:
        t.join()
        os.close(r)


def test_raw_lines_splits_lines_across_reads():
    lines = _pipe_lines([b'{"a": 1}\n{"b"', b': 2}\n\n{"c": 3}'], chunk_size=4)
    assert lines == [b'{"a": 1}\n', b'{"b": 2}\n', b"\n", b'{"c": 3}']


def test_raw_lines_keeps_a_long_line_whole():
    big = b"x" * 300_000
    assert _pipe_lines([big + b"\n", b"tail\n"], chunk_size=4096) == [big + b"\n", b"tail\n"]


def test_raw_lines_keeps_utf8_bytes_for_the_intake_to_decode():
    line = json.dumps({"content": "café 🍁"}, ensure_ascii=False).encode("utf-8") + b"\n"
    assert _pipe_lines([line[:9], line[9:]], chunk_size=3) == [line]


def test_raw_lines_refuses_a_non_blocking_stream():
    with pytest.raises(BlockingIOError):
        list(_raw_lines(lambda n: None))


def test_stdin_lines_reads_a_real_stdin_through_its_raw_stream():
    r, w = os.pipe()
    os.write(w, b'{"type": "shutdown"}\n')
    os.close(w)
    with io.TextIOWrapper(io.BufferedReader(io.FileIO(r, "rb"))) as stdin:
        lines = _stdin_lines(stdin)
        assert lines is not stdin.buffer
        assert list(lines) == [b'{"type": "shutdown"}\n']


@pytest.mark.parametrize("stream", [io.StringIO("x\n"), io.BytesIO(b"x\n")])
def test_stdin_lines_uses_a_stream_without_a_raw_layer_as_is(stream):
    assert _stdin_lines(stream) is stream
