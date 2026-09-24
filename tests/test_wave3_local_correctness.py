"""Regression tests for Wave 3 (surface-local correctness).

Each test is built from its issue's repro and driven through the sidecar's own entry
points (``run()`` / ``main()`` / a ``python -m langstage_vscode`` subprocess):

- gh #119 (+ #136): incoming stdio is decoded as UTF-8, whatever the locale; a line
  that is not UTF-8 becomes an ``error`` frame instead of killing the reader thread.
- gh #113 / #122 / #134: a ``message`` with a non-string ``content`` or ``session_id``,
  or one sent while its session is paused on an interrupt, is rejected with
  ``error -> turn_end`` (the gh #118 rule for rejected commands), never a raw
  Pydantic dump or a silently swallowed message.
- gh #121: a workspace root that is an existing file is a clean config error.
- gh #124: ``--selfcheck`` doesn't report healthy by validating the demo stub when a
  typo'd ``langstage.toml`` key left the agent unconfigured.
- gh #116: a dotted ``module:graph`` spec in the workspace passes the console-script
  preflight even when it is launched from outside the workspace.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from langstage_core import load_agent_spec

from langstage_vscode.sidecar import main, run

_AGENT_SRC = (
    "from langgraph.graph import StateGraph, START, END, MessagesState\n"
    "from langchain_core.messages import AIMessage\n"
    "def r(state):\n"
    "    return {'messages': [AIMessage(content=%r)]}\n"
    "_b = StateGraph(MessagesState)\n"
    "_b.add_node('r', r); _b.add_edge(START, 'r'); _b.add_edge('r', END)\n"
    "graph = _b.compile()\n"
)


def _stub():
    return load_agent_spec("langstage_core.demo.stub:graph")


def _isolate(monkeypatch, tmp_path) -> None:
    """cwd = tmp_path, an empty global config home, no LANGSTAGE_*/DEEPAGENT_* env."""
    monkeypatch.chdir(tmp_path)
    gh = tmp_path / "_global"
    gh.mkdir(exist_ok=True)
    monkeypatch.setenv("LANGSTAGE_CONFIG_HOME", str(gh))
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT", "LANGSTAGE_DEBUG", "DEEPAGENT_DEBUG",
                "DEEPAGENTS_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)


def _frames(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def _drive(graph, commands) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(c) + "\n" for c in commands))
    out = io.StringIO()
    run(graph, stdin, out)
    return _frames(out.getvalue())


def _sidecar_env(tmp_path: Path, **extra: str) -> dict:
    env = dict(os.environ)
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT", "PYTHONUTF8"):
        env.pop(var, None)
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    env.update(extra)
    return env


# ── gh #119 / #136: UTF-8 stdin ──────────────────────────────────────────────


def _raw_stdio(tmp_path: Path, payload: bytes) -> subprocess.CompletedProcess:
    """Drive the raw stdio loop (the extension's path) over a cp1252 stdio pipe — the
    Western-Windows default the issue reproduces with PYTHONIOENCODING=cp1252."""
    return subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--demo"],
        input=payload, capture_output=True, timeout=120, cwd=str(tmp_path),
        env=_sidecar_env(tmp_path, PYTHONIOENCODING="cp1252"),
    )


def _msg(content: str, session_id: str = "s1") -> bytes:
    # What the extension writes: JSON.stringify leaves non-ASCII literal -> UTF-8 bytes.
    return (json.dumps({"type": "message", "session_id": session_id, "content": content},
                       ensure_ascii=False) + "\n").encode("utf-8")


def _echoed(frames: list[dict], session_id: str) -> str:
    """The content the echo stub streamed for one session's turn."""
    out: list[str] = []
    for f in frames:
        if f.get("type") == "turn_end":
            if f.get("session_id") == session_id:
                return "".join(out)
            out = []
        elif f.get("type") == "content":
            out.append(f.get("content", ""))
    return "".join(out)


def test_non_ascii_message_reaches_agent_intact_on_cp1252_stdio(tmp_path):
    """gh #119: `café 世界 🚀` used to reach the agent as `cafÃ© ä¸–ç•Œ ðŸš€`."""
    sent = "café 世界 🚀"
    proc = _raw_stdio(tmp_path, _msg(sent) + b'{"type":"shutdown"}\n')
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    frames = _frames(proc.stdout.decode("utf-8"))
    assert sent in _echoed(frames, "s1")


def test_undefined_cp1252_byte_does_not_kill_the_sidecar(tmp_path):
    """gh #136: `🍁` (F0 9F 8D 81) has a byte cp1252 leaves undefined, which raised
    UnicodeDecodeError in the reader thread and ended the whole process with no
    `error`/`turn_end`. Now the message goes through and the next one does too."""
    payload = _msg("maple 🍁", "s1") + _msg("after", "s2") + b'{"type":"shutdown"}\n'
    proc = _raw_stdio(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    frames = _frames(proc.stdout.decode("utf-8"))
    assert "maple 🍁" in _echoed(frames, "s1")
    assert [f.get("session_id") for f in frames if f["type"] == "turn_end"] == ["s1", "s2"]


def test_invalid_utf8_line_is_an_error_frame_not_a_silent_exit(tmp_path):
    """A line that isn't UTF-8 is a bad command like invalid JSON: one `error` frame,
    and the loop keeps serving."""
    payload = b'{"type":"message","session_id":"s1","content":"\xff\xfe"}\n' + _msg("ok", "s2") \
        + b'{"type":"shutdown"}\n'
    proc = _raw_stdio(tmp_path, payload)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    frames = _frames(proc.stdout.decode("utf-8"))
    errors = [f for f in frames if f["type"] == "error"]
    assert len(errors) == 1 and "UTF-8" in errors[0]["error"]
    assert "ok" in _echoed(frames, "s2")


def test_run_accepts_a_binary_stream_with_cancel_enabled():
    """main() hands run() the binary stdin; the background reader decodes each line."""
    stdin = io.BytesIO(_msg("héllo") + b"\xff\n" + b'{"type":"shutdown"}\n')
    out = io.StringIO()
    run(_stub(), stdin, out, enable_cancel=True)
    frames = _frames(out.getvalue())
    assert "héllo" in _echoed(frames, "s1")
    assert [f["type"] for f in frames].count("error") == 1


# ── gh #113 / #122 / #134: validated `message` commands ─────────────────────


def _rejected(frames: list[dict]) -> tuple[dict, dict]:
    """Assert the gh #118 rejection shape (ready, error, turn_end; no ack) and return
    the error + turn_end frames."""
    types = [f["type"] for f in frames]
    assert types == ["ready", "error", "turn_end"], frames
    return frames[1], frames[2]


def test_non_string_content_is_a_one_line_error():
    """gh #113: `content: 123` / `true` leaked a 6-line UserMessage ValidationError."""
    for bad in (123, True, {"text": "hi"}):
        err, end = _rejected(_drive(_stub(), [
            {"type": "message", "session_id": "s1", "content": bad}, {"type": "shutdown"},
        ]))
        assert err["error"] == "message 'content' must be a string"
        assert "\n" not in err["error"] and "ValidationError" not in err["error"]
        assert end == {"type": "turn_end", "session_id": "s1"}


def test_non_string_session_id_is_a_one_line_error_and_not_echoed():
    """gh #122: `session_id: null` / `99` leaked a RunAgentInput/thread_id error and
    echoed the non-string back in `turn_end`."""
    for bad in (None, 99, ["a"]):
        err, end = _rejected(_drive(_stub(), [
            {"type": "message", "session_id": bad, "content": "hi"}, {"type": "shutdown"},
        ]))
        assert err["error"] == "message 'session_id' must be a string"
        assert end == {"type": "turn_end"}  # no non-string session_id echoed back


def test_non_string_session_id_on_decision_and_cancel_does_not_crash():
    """The same guard covers `decision` (an unhashable id used to raise TypeError in
    the pending-interrupt lookup and kill the loop) and `cancel`."""
    frames = _drive(_stub(), [
        {"type": "decision", "session_id": ["a"], "decisions": [{"type": "approve"}]},
        {"type": "cancel", "session_id": {"x": 1}},
        {"type": "message", "session_id": "s1", "content": "still alive"},
        {"type": "shutdown"},
    ])
    types = [f["type"] for f in frames]
    assert types[:4] == ["ready", "error", "turn_end", "error"], frames
    assert frames[1]["error"] == "decision 'session_id' must be a string"
    assert frames[3]["error"] == "cancel 'session_id' must be a string"
    assert "still alive" in _echoed(frames, "s1")


def test_absent_session_id_still_defaults():
    frames = _drive(_stub(), [{"type": "message", "content": "hi"}, {"type": "shutdown"}])
    assert frames[-1] == {"type": "turn_end", "session_id": "default"}


def _hitl_count_graph():
    """The issue's repro agent: pauses, then reports the human messages it saw."""
    def node(state):
        ans = interrupt({"question": "approve?"})
        humans = [m.content for m in state["messages"] if isinstance(m, HumanMessage)]
        return {"messages": [AIMessage(content=f"humans={humans}; resume={ans}")]}

    b = StateGraph(MessagesState)
    b.add_node("node", node)
    b.add_edge(START, "node")
    b.add_edge("node", END)
    return b.compile(checkpointer=MemorySaver())


def test_message_while_interrupt_pending_is_rejected_not_swallowed():
    """gh #134: a `message` to a paused session was acked, re-interrupted, and its
    content silently discarded. Now it is rejected with error -> turn_end, the session
    stays paused, and a `decision` still resumes it."""
    frames = _drive(_hitl_count_graph(), [
        {"type": "message", "session_id": "s1", "content": "first"},
        {"type": "message", "session_id": "s1", "content": "ORPHAN sent while paused"},
        {"type": "decision", "session_id": "s1", "decisions": [{"type": "approve"}]},
        {"type": "shutdown"},
    ])
    turn_ends = [i for i, f in enumerate(frames) if f["type"] == "turn_end"]
    assert len(turn_ends) == 3
    second = frames[turn_ends[0] + 1: turn_ends[1] + 1]
    assert [f["type"] for f in second] == ["error", "turn_end"], second
    assert "pending interrupt" in second[0]["error"] and "decision" in second[0]["error"]
    assert "'s1'" in second[0]["error"]
    third = frames[turn_ends[1] + 1: turn_ends[2] + 1]
    assert third[0] == {"type": "ack", "ref": "decision"}
    reply = "".join(f.get("content", "") for f in third if f["type"] == "content")
    assert "humans=['first']" in reply


def test_message_to_a_different_session_is_not_blocked_by_anothers_interrupt():
    frames = _drive(_hitl_count_graph(), [
        {"type": "message", "session_id": "s1", "content": "first"},
        {"type": "message", "session_id": "s2", "content": "other chat"},
        {"type": "shutdown"},
    ])
    assert [f["type"] for f in frames].count("ack") == 2
    assert "error" not in [f["type"] for f in frames]


# ── gh #121: a workspace root that is a file ─────────────────────────────────


def test_workspace_root_that_is_a_file_is_a_clean_error_frame(monkeypatch, tmp_path, capsys):
    """Raw stdio: one `error` frame on stdout, exit 1, no traceback."""
    _isolate(monkeypatch, tmp_path)
    afile = tmp_path / "afile.txt"
    afile.write_text("x")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = main(["--demo", "--workspace", str(afile)])
    assert rc == 1
    out = _frames(capsys.readouterr().out)
    assert len(out) == 1 and out[0]["type"] == "error"
    assert "is not a directory" in out[0]["error"] and "afile.txt" in out[0]["error"]


def test_workspace_root_that_is_a_file_fails_selfcheck_json(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    afile = tmp_path / "afile.txt"
    afile.write_text("x")
    rc = main(["--demo", "--workspace", str(afile), "--selfcheck", "--json"])
    assert rc == 1
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["ok"] is False and "is not a directory" in verdict["message"]


def test_workspace_root_that_is_a_file_message_mode_uses_stderr(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    afile = tmp_path / "afile.txt"
    afile.write_text("x")
    monkeypatch.setenv("LANGSTAGE_WORKSPACE_ROOT", str(afile))
    rc = main(["--demo", "--message", "hi"])
    assert rc == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err.startswith("error: workspace root") and "is not a directory" in cap.err


def test_missing_workspace_root_is_still_created(monkeypatch, tmp_path):
    """Creating a missing root is intended (triage); only a file there is an error."""
    _isolate(monkeypatch, tmp_path)
    ws = tmp_path / "new" / "ws"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["--demo", "--workspace", str(ws)]) == 0
    assert ws.is_dir()


# ── gh #124: --selfcheck and the demo-stub fallback ──────────────────────────


def _agent_project(tmp_path: Path, toml: str, name: str = "proj") -> Path:
    proj = tmp_path / name
    proj.mkdir()
    (proj / "my_agent.py").write_text(_AGENT_SRC % "hi", encoding="utf-8")
    (proj / "langstage.toml").write_text(toml, encoding="utf-8")
    return proj


def test_selfcheck_fails_on_typod_spec_key_instead_of_validating_the_stub(
    monkeypatch, tmp_path, capsys
):
    for toml, key in (('[agent]\nspecc = "my_agent.py:graph"\n', "agent.specc"),
                      ('[agents]\nspec = "my_agent.py:graph"\n', "agents.spec")):
        _isolate(monkeypatch, tmp_path)
        proj = _agent_project(tmp_path, toml, name=key)
        monkeypatch.chdir(proj)
        rc = main(["--selfcheck", "--json"])
        verdict = json.loads(capsys.readouterr().out.strip())
        assert rc == 1, verdict
        assert verdict["ok"] is False
        assert key in verdict["message"] and "demo stub" in verdict["message"]


def test_selfcheck_with_no_config_says_it_only_validated_the_stub(monkeypatch, tmp_path, capsys):
    """The documented no-agent fallback stays OK, but says what it did validate."""
    _isolate(monkeypatch, tmp_path)
    assert main(["--selfcheck", "--json"]) == 0
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["ok"] is True and verdict["demo_fallback"] is True
    assert "no agent configured" in verdict["message"]


def test_selfcheck_with_a_real_agent_is_not_flagged_as_fallback(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    proj = _agent_project(tmp_path, '[agent]\nspec = "my_agent.py:graph"\n')
    monkeypatch.chdir(proj)
    assert main(["--selfcheck", "--json"]) == 0
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["ok"] is True and "demo_fallback" not in verdict


# ── gh #116: dotted spec preflight from outside the workspace ────────────────


def test_console_script_selfcheck_finds_a_dotted_spec_in_the_workspace(tmp_path):
    """The extension spawns with cwd == workspace, so `python -m` has the workspace on
    sys.path and `my_agent:graph` loads in chat. The console script launched from
    elsewhere with --workspace used to false-FAIL with ModuleNotFoundError."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "my_agent_w3.py").write_text(_AGENT_SRC % "hello from my_agent", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # A console script's sys.path[0] is its own bin/ directory, not the cwd or the
    # workspace. A runner script in a separate directory reproduces exactly that.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    runner = bindir / "sidecar_runner.py"
    runner.write_text(
        "import sys\nfrom langstage_vscode.sidecar import main\nsys.exit(main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    for extra in (["--selfcheck"], ["--message", "hi"]):
        proc = subprocess.run(
            [sys.executable, str(runner), "--agent", "my_agent_w3:graph",
             "--workspace", str(ws), *extra],
            capture_output=True, text=True, cwd=str(elsewhere), timeout=120,
            env=_sidecar_env(tmp_path),
        )
        assert proc.returncode == 0, proc.stderr
    assert "hello from my_agent" in proc.stdout


def test_dotted_spec_missing_everywhere_still_fails_cleanly(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    rc = main(["--selfcheck", "--agent", "no_such_mod_w3:graph", "--workspace", str(ws)])
    assert rc == 1
    assert "ModuleNotFoundError" in capsys.readouterr().err
