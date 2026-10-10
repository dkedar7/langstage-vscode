"""gh #158: over the raw stdio loop, an agent spec that loads but can't be served exits 1.

A spec that loads to something that isn't a runnable graph (an uncompiled ``StateGraph``,
the classic forgot-``.compile()`` mistake; a factory function; a bare ``int``), or whose
agent build fails, gets ``ready`` (the handshake, no ``checkpointer``) and then the
startup ``error`` frame. The process used to exit 0 after those frames, while a spec that
fails to load (a ``str`` value, which core refuses) exits 1, and so do ``--selfcheck`` and
``--message`` for all of them. It now exits 1 too, with the frames unchanged.

What does NOT change: a served loop that ends on ``shutdown`` or EOF exits 0, even when a
turn errored along the way (the loop answers it with an ``error`` frame and keeps serving).

Most of these spawn the real ``python -m langstage_vscode`` the way the extension does.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from langstage_vscode.sidecar import main
from tests.test_ready_handshake import _assert_handshake
from tests.test_sidecar import _isolate_config

_SHUTDOWN = b'{"type": "shutdown"}\n'
_TIMEOUT = 120

_UNCOMPILED_STATEGRAPH = """\
from typing import TypedDict
from langgraph.graph import END, START, StateGraph

class S(TypedDict):
    x: int

b = StateGraph(S)
b.add_node("n", lambda s: s)
b.add_edge(START, "n")
b.add_edge("n", END)
graph = b  # forgot .compile()
"""

_FACTORY_FUNCTION = """\
from langgraph.graph import END, START, MessagesState, StateGraph

def graph():
    b = StateGraph(MessagesState)
    b.add_node("n", lambda s: s)
    b.add_edge(START, "n")
    b.add_edge("n", END)
    return b.compile()
"""

_BARE_INT = "graph = 42\n"

# Has a callable `.stream`, so it passes the runnable check, but it isn't a compiled
# graph: core's build_agent refuses it. The other branch into the startup `error` frame.
_NOT_A_GRAPH_WITH_STREAM = """\
class NotAGraph:
    def stream(self, *args, **kwargs):
        return iter(())

graph = NotAGraph()
"""

_BARE_STR = 'graph = "oops"\n'

_RAISES_MID_TURN = """\
from langgraph.graph import END, START, MessagesState, StateGraph

def boom(state):
    raise RuntimeError("kaboom")

b = StateGraph(MessagesState)
b.add_node("boom", boom)
b.add_edge(START, "boom")
b.add_edge("boom", END)
graph = b.compile()
"""


def _env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT", "LANGSTAGE_DEBUG"):
        env.pop(var, None)
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    return env


def _sidecar(tmp_path: Path, args: list[str], stdin: bytes = _SHUTDOWN) -> tuple[int, list[dict], str]:
    """Run the real stdio loop (no --message/--repl/--selfcheck) with ``stdin``.
    Returns (exit code, stdout frames, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", *args],
        input=stdin, capture_output=True, cwd=str(tmp_path), env=_env(tmp_path),
        timeout=_TIMEOUT, check=False,
    )
    frames = [json.loads(ln) for ln in proc.stdout.decode("utf-8").splitlines() if ln.strip()]
    return proc.returncode, frames, proc.stderr.decode("utf-8", "replace")


def _agent(tmp_path: Path, source: str) -> list[str]:
    (tmp_path / "agent.py").write_text(source, encoding="utf-8")
    return ["--agent", "./agent.py:graph"]


@pytest.mark.parametrize(
    ("source", "expected_error"),
    [
        pytest.param(_UNCOMPILED_STATEGRAPH, "loaded a `StateGraph`, not a runnable graph",
                     id="uncompiled-stategraph"),
        pytest.param(_FACTORY_FUNCTION, "loaded a `function`, not a runnable graph",
                     id="factory-function"),
        pytest.param(_BARE_INT, "loaded a `int`, not a runnable graph", id="bare-int"),
        pytest.param(_NOT_A_GRAPH_WITH_STREAM, "build_agent expected a compiled LangGraph graph",
                     id="agent-build-fails"),
    ],
)
def test_an_unservable_agent_gets_ready_then_error_and_exits_1(tmp_path, source, expected_error):
    rc, frames, stderr = _sidecar(tmp_path, _agent(tmp_path, source))
    assert rc == 1, stderr
    # The frames are unchanged: `ready` with the handshake and no checkpointer, then the
    # startup error, then nothing.
    assert [f["type"] for f in frames] == ["ready", "error"], frames
    _assert_handshake(frames[0])
    assert "checkpointer" not in frames[0]
    assert expected_error in frames[1]["error"], frames[1]


def test_a_bare_str_value_still_fails_to_load_and_exits_1(tmp_path):
    # The sibling case, unchanged: core's loader refuses a str, so this is a load failure,
    # one `error` frame with no `ready` (the README's "Startup failures").
    rc, frames, stderr = _sidecar(tmp_path, _agent(tmp_path, _BARE_STR))
    assert rc == 1, stderr
    assert [f["type"] for f in frames] == ["error"], frames
    assert "failed to load agent" in frames[0]["error"] and "resolved to a str" in frames[0]["error"]


def test_an_unservable_agent_exits_1_with_stdin_held_open(tmp_path):
    # The extension keeps the pipe open and waits for the exit: the sidecar must not
    # wait for a command it will never serve.
    proc = subprocess.Popen(
        [sys.executable, "-m", "langstage_vscode", *_agent(tmp_path, _UNCOMPILED_STATEGRAPH)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(tmp_path), env=_env(tmp_path),
    )
    try:
        rc = proc.wait(timeout=_TIMEOUT)
        out = proc.stdout.read().decode("utf-8")
        err = proc.stderr.read().decode("utf-8", "replace")
    finally:
        if proc.poll() is None:
            proc.kill()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
    assert rc == 1, err
    assert [json.loads(ln)["type"] for ln in out.splitlines() if ln.strip()] == ["ready", "error"]


def test_a_served_demo_that_shuts_down_exits_0(tmp_path):
    rc, frames, stderr = _sidecar(tmp_path, ["--demo"])
    assert rc == 0, stderr
    assert [f["type"] for f in frames] == ["ready"], frames
    _assert_handshake(frames[0])
    assert "checkpointer" in frames[0]


def test_a_turn_error_does_not_change_the_loops_exit_code(tmp_path):
    # The agent raises mid-turn: the loop answers with `error -> turn_end`, keeps serving
    # (the next turn runs too), and a clean shutdown still exits 0.
    turn = json.dumps({"type": "message", "session_id": "s", "content": "go"}).encode() + b"\n"
    rc, frames, stderr = _sidecar(
        tmp_path, _agent(tmp_path, _RAISES_MID_TURN), stdin=turn + turn + _SHUTDOWN,
    )
    assert rc == 0, stderr
    types = [f["type"] for f in frames]
    assert types == ["ready", "ack", "error", "turn_end", "ack", "error", "turn_end"], types
    assert "kaboom" in frames[2]["error"]


# ---- the same contract through main() in-process ----------------------------------


@pytest.mark.parametrize(
    "source",
    [_UNCOMPILED_STATEGRAPH, _FACTORY_FUNCTION, _BARE_INT],
    ids=["uncompiled-stategraph", "factory-function", "bare-int"],
)
def test_main_returns_1_for_an_unservable_agent(monkeypatch, tmp_path, capsys, source):
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(_SHUTDOWN.decode()))
    assert main(_agent(tmp_path, source)) == 1
    out = capsys.readouterr().out
    assert [json.loads(ln)["type"] for ln in out.splitlines() if ln.strip()] == ["ready", "error"]


def test_main_returns_0_when_a_served_loop_shuts_down(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(_SHUTDOWN.decode()))
    assert main(["--demo"]) == 0
    out = capsys.readouterr().out
    assert [json.loads(ln)["type"] for ln in out.splitlines() if ln.strip()] == ["ready"]
