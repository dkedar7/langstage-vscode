"""gh #189: what the agent's process tree writes to fd 1 goes to stderr, not the wire.

gh #187 swapped ``sys.stdout`` for the turn, which catches Python-level prints, but a
child process the agent starts (``os.system``, ``subprocess.run`` without
``capture_output``) inherits file descriptor 1, and native code writes to it directly.
Both landed on the NDJSON frame wire; a write with no trailing newline was glued onto
the next frame, which the panel then dropped as non-JSON. The sidecar now moves the
wire off fd 1 at startup and points fd 1 at stderr. Each path is driven as a real
subprocess, as the extension spawns it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AGENT = '''
import os, subprocess, sys
from langgraph.graph import StateGraph, MessagesState, START
from langchain_core.messages import AIMessage

PY = {py!r}

def n(s):
    child = [PY, "-c", "import sys; sys.stdout.write('child-subprocess'); sys.stdout.flush()"]
    subprocess.run(child)                                    # a tool that doesn't capture
    cmd = subprocess.list2cmdline([PY, "-c", "import sys; sys.stdout.write('child-ossystem')"])
    os.system('"' + cmd + '"' if os.name == "nt" else cmd)   # cmd.exe strips outer quotes
    os.write(1, b"native-fd-write")                          # what a C extension does
    return {{"messages": [AIMessage(content="ok")]}}

g = StateGraph(MessagesState); g.add_node("n", n); g.add_edge(START, "n"); graph = g.compile()
'''

MARKERS = ("child-subprocess", "child-ossystem", "native-fd-write")


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    (tmp_path / "shells.py").write_text(AGENT.format(py=sys.executable), encoding="utf-8")
    (tmp_path / "_global").mkdir()
    return tmp_path


def _sidecar(cwd: Path, args: list[str], stdin: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT", "LANGSTAGE_DEBUG", "DEEPAGENT_DEBUG"):
        env.pop(var, None)
    env["LANGSTAGE_CONFIG_HOME"] = str(cwd / "_global")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--agent", "shells.py:graph", *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8",
        cwd=cwd, env=env, timeout=120,
    )


def _ndjson(stdout: str) -> list[dict]:
    """Every stdout line must be one JSON frame; a non-JSON line fails the test."""
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def _assert_children_on_stderr(proc: subprocess.CompletedProcess) -> None:
    for marker in MARKERS:
        assert marker not in proc.stdout, (marker, proc.stdout)
        assert marker in proc.stderr, (marker, proc.stderr)


def test_stdio_loop_frames_survive_child_output(agent_dir: Path) -> None:
    commands = (
        json.dumps({"type": "message", "session_id": "s1", "content": "hi"}) + "\n"
        + json.dumps({"type": "shutdown"}) + "\n"
    )
    proc = _sidecar(agent_dir, [], stdin=commands)
    assert proc.returncode == 0, proc.stderr
    frames = _ndjson(proc.stdout)
    types = [f["type"] for f in frames]
    assert types[:2] == ["ready", "ack"], types
    assert types[-2:] == ["complete", "turn_end"], types
    content = "".join(f.get("content", "") for f in frames if f["type"] == "content")
    assert content == "ok", frames
    _assert_children_on_stderr(proc)


def test_message_reply_is_exactly_the_reply(agent_dir: Path) -> None:
    proc = _sidecar(agent_dir, ["--message", "hi"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok", proc.stdout
    _assert_children_on_stderr(proc)


def test_message_json_is_valid_ndjson(agent_dir: Path) -> None:
    proc = _sidecar(agent_dir, ["--message", "hi", "--json"])
    assert proc.returncode == 0, proc.stderr
    frames = _ndjson(proc.stdout)
    assert "".join(f.get("content", "") for f in frames if f["type"] == "content") == "ok"
    _assert_children_on_stderr(proc)


def test_repl_reply_is_exactly_the_reply(agent_dir: Path) -> None:
    proc = _sidecar(agent_dir, ["--repl"], stdin="hi\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok", proc.stdout
    _assert_children_on_stderr(proc)


def test_selfcheck_json_is_one_clean_verdict(agent_dir: Path) -> None:
    proc = _sidecar(agent_dir, ["--selfcheck", "--json"])
    assert proc.returncode == 0, proc.stderr
    (verdict,) = _ndjson(proc.stdout)
    assert verdict["type"] == "selfcheck" and verdict["ok"] is True, verdict
    _assert_children_on_stderr(proc)


def test_wire_has_no_stray_newline_translation(agent_dir: Path) -> None:
    """The moved wire writes frames exactly as before: one frame per line."""
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--demo", "--message", "hi", "--json"],
        capture_output=True, cwd=agent_dir, timeout=120,
        env={**os.environ, "LANGSTAGE_CONFIG_HOME": str(agent_dir / "_global")},
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines and all(json.loads(ln) for ln in lines)
    expected_eol = b"\r\n" if os.name == "nt" else b"\n"
    assert proc.stdout.endswith(expected_eol), proc.stdout[-10:]
    assert b"\r\r\n" not in proc.stdout
