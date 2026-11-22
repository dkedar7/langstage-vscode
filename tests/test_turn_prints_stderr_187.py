"""gh #187: what an agent prints DURING a turn goes to stderr, never onto stdout.

stdout is the NDJSON frame wire (the stdio loop, ``--message --json``, ``--selfcheck
--json``) or the clean reply channel (``--message``, ``--repl``). Import-time prints
were already diverted; a node's ``print("working...", end="")`` used to glue onto the
front of the next frame, so the panel dropped that frame (here, the whole reply) as
non-JSON noise. Each path is driven as a real subprocess, as the extension spawns it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AGENT = '''
from langgraph.graph import StateGraph, MessagesState, START
from langchain_core.messages import AIMessage

def n(s):
    print("working...", end="", flush=True)  # a progress indicator / chatty library
    print("inside node")                      # a debug print with a newline
    return {"messages": [AIMessage(content="ok")]}

g = StateGraph(MessagesState); g.add_node("n", n); g.add_edge(START, "n"); graph = g.compile()
'''


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    (tmp_path / "partial.py").write_text(AGENT, encoding="utf-8")
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
        [sys.executable, "-m", "langstage_vscode", "--agent", "partial.py:graph", *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8",
        cwd=cwd, env=env, timeout=120,
    )


def _ndjson(stdout: str) -> list[dict]:
    """Every stdout line must be one JSON frame; a non-JSON line fails the test."""
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def _assert_prints_on_stderr(proc: subprocess.CompletedProcess) -> None:
    assert "working..." not in proc.stdout, proc.stdout
    assert "inside node" not in proc.stdout, proc.stdout
    assert "working..." in proc.stderr, proc.stderr
    assert "inside node" in proc.stderr, proc.stderr


def test_stdio_loop_frames_survive_a_partial_print(agent_dir: Path) -> None:
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
    _assert_prints_on_stderr(proc)


def test_message_reply_is_exactly_the_reply(agent_dir: Path) -> None:
    proc = _sidecar(agent_dir, ["--message", "hi"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok", proc.stdout
    _assert_prints_on_stderr(proc)


def test_message_json_is_valid_ndjson(agent_dir: Path) -> None:
    proc = _sidecar(agent_dir, ["--message", "hi", "--json"])
    assert proc.returncode == 0, proc.stderr
    frames = _ndjson(proc.stdout)
    assert "".join(f.get("content", "") for f in frames if f["type"] == "content") == "ok"
    _assert_prints_on_stderr(proc)


def test_repl_reply_is_exactly_the_reply(agent_dir: Path) -> None:
    proc = _sidecar(agent_dir, ["--repl"], stdin="hi\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok", proc.stdout
    _assert_prints_on_stderr(proc)


def test_selfcheck_json_is_one_clean_verdict(agent_dir: Path) -> None:
    proc = _sidecar(agent_dir, ["--selfcheck", "--json"])
    assert proc.returncode == 0, proc.stderr
    (verdict,) = _ndjson(proc.stdout)
    assert verdict["type"] == "selfcheck" and verdict["ok"] is True, verdict
    _assert_prints_on_stderr(proc)
