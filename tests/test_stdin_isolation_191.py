"""gh #191: only the sidecar reads the command pipe; the agent's stdin is EOF.

In the stdio loop, stdin carries the panel's commands. A child process the agent
starts inherits fd 0 (a CLI that prompts with no TTY, ``read``, ``head``), and agent
code can call ``input()`` or ``sys.stdin.readline()``. Either swallowed the next
command, which then got no ``ack`` and no ``turn_end``, so the conversation stalled.
The sidecar now reads commands from a private descriptor and points fd 0 and
``sys.stdin`` at devnull. Each case runs the sidecar as a real subprocess, as the
extension spawns it, and sends the next command only while the agent is reading.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

AGENT = '''
import subprocess, sys, time
from langgraph.graph import StateGraph, MessagesState, START
from langchain_core.messages import AIMessage

PY = {py!r}
MODE = {mode!r}

def n(s):
    if MODE == "child":
        # A tool that shells out to a CLI which reads stdin (a prompt, `head -n 1`).
        # It announces itself on stderr, so the test sends the next command while the
        # child is reading.
        child = [PY, "-c", "import sys; sys.stderr.write('child-reading\\\\n'); "
                 "sys.stderr.flush(); print(repr(sys.stdin.readline()))"]
        out = subprocess.run(child, stdout=subprocess.PIPE, text=True).stdout.strip()
        return {{"messages": [AIMessage(content="child read: " + out)]}}
    sys.stderr.write("node-reading\\n"); sys.stderr.flush()
    line = sys.stdin.readline()
    try:
        prompt = input()
    except EOFError:
        prompt = "<EOF>"
    return {{"messages": [AIMessage(content=f"readline={{line!r}} input={{prompt}}")]}}

g = StateGraph(MessagesState); g.add_node("n", n); g.add_edge(START, "n"); graph = g.compile()
'''


def _agent_dir(tmp_path: Path, mode: str) -> Path:
    (tmp_path / "reader.py").write_text(AGENT.format(py=sys.executable, mode=mode), encoding="utf-8")
    (tmp_path / "_global").mkdir(exist_ok=True)
    return tmp_path


class _Sidecar:
    """The stdio loop as a live subprocess: frames and stderr lines are queued as they
    arrive, so a test can send a command at a precise moment."""

    def __init__(self, cwd: Path) -> None:
        env = dict(os.environ)
        for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                    "DEEPAGENT_WORKSPACE_ROOT", "LANGSTAGE_DEBUG", "DEEPAGENT_DEBUG"):
            env.pop(var, None)
        env["LANGSTAGE_CONFIG_HOME"] = str(cwd / "_global")
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "langstage_vscode", "--agent", "reader.py:graph"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=cwd, env=env, bufsize=1,
        )
        self.frames: queue.Queue[dict] = queue.Queue()
        self.stderr: queue.Queue[str] = queue.Queue()
        self.all_frames: list[dict] = []
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            if line.strip():
                self.frames.put(json.loads(line))  # a non-JSON line fails the test

    def _pump_err(self) -> None:
        for line in self.proc.stderr:  # type: ignore[union-attr]
            self.stderr.put(line)

    def send(self, **cmd: object) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()

    def wait_frame(self, pred, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"frame never arrived; got {self.all_frames}")
            try:
                frame = self.frames.get(timeout=left)
            except queue.Empty:
                continue
            self.all_frames.append(frame)
            if pred(frame):
                return frame

    def wait_stderr(self, marker: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if marker in self.stderr.get(timeout=0.5):
                    return
            except queue.Empty:
                continue
        raise AssertionError(f"{marker!r} never appeared on stderr")

    def turn_end(self, sid: str) -> dict:
        return self.wait_frame(lambda f: f["type"] == "turn_end" and f.get("session_id") == sid)

    def close(self) -> int:
        try:
            self.send(type="shutdown")
            self.proc.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=60)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()

    def content(self) -> str:
        return "".join(f.get("content", "") for f in self.all_frames if f["type"] == "content")


@pytest.fixture
def sidecar_for(tmp_path: Path):
    started: list[_Sidecar] = []

    def start(mode: str) -> _Sidecar:
        sc = _Sidecar(_agent_dir(tmp_path, mode))
        started.append(sc)
        sc.wait_frame(lambda f: f["type"] == "ready")
        return sc

    yield start
    for sc in started:
        if sc.proc.poll() is None:
            sc.proc.kill()


def test_child_process_reading_stdin_gets_eof_not_the_next_command(sidecar_for) -> None:
    sc = sidecar_for("child")
    sc.send(type="message", session_id="s1", content="first")
    sc.wait_stderr("child-reading")
    sc.send(type="message", session_id="s2", content="second")  # sent while the child reads
    sc.turn_end("s1")
    sc.turn_end("s2")  # used to never arrive: the child had consumed this command
    sc.send(type="message", session_id="s3", content="third")
    sc.turn_end("s3")
    assert sc.close() == 0
    text = sc.content()
    assert text.count("child read: ''") == 3, text
    assert '"type"' not in text, text  # the agent was never fed protocol JSON


def test_agent_stdin_readline_and_input_see_eof(sidecar_for) -> None:
    sc = sidecar_for("python")
    sc.send(type="message", session_id="s1", content="first")
    sc.wait_stderr("node-reading")
    sc.send(type="message", session_id="s2", content="second")
    sc.turn_end("s1")
    sc.turn_end("s2")
    assert sc.close() == 0  # shutdown reached the sidecar, not the agent's readline
    text = sc.content()
    assert text.count("readline='' input=<EOF>") == 2, text


def test_message_cancel_message_are_all_answered(sidecar_for) -> None:
    """The issue's panel flow: a tool reads stdin, the user clicks Stop, then sends the
    next message. The next message used to get no frames at all."""
    sc = sidecar_for("child")
    sc.send(type="message", session_id="s1", content="install it")
    sc.wait_stderr("child-reading")
    sc.send(type="cancel", session_id="s1")
    sc.turn_end("s1")
    sc.send(type="message", session_id="s1", content="hello again")
    sc.wait_frame(lambda f: f["type"] == "ack")
    sc.turn_end("s1")
    assert sc.close() == 0
    assert '"type"' not in sc.content(), sc.content()
