"""gh #89: every `ready` frame carries a handshake — the sidecar's `version`, its
`protocol` version and its `capabilities` — so the extension can tell an outdated or
incompatible sidecar before it sends a command, instead of failing on the first one.

`protocol` is the breaking-change channel, `capabilities` the additive one. The same three
fields are in the `--selfcheck --json` verdict, for a script that doesn't drive the loop.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

import langstage_vscode
from langstage_vscode.sidecar import CAPABILITIES, PROTOCOL_VERSION, main, run
from tests.test_sidecar import _isolate_config

README = Path(__file__).resolve().parent.parent / "README.md"

EXPECTED_CAPABILITIES = ["message", "decision", "cancel", "shutdown", "checkpointer"]

# What each capability names: a command the stdio loop handles, or a field of `ready`.
COMMAND_CAPABILITIES = {"message", "decision", "cancel", "shutdown"}
READY_FIELD_CAPABILITIES = {"checkpointer"}


def _graph():
    b = StateGraph(MessagesState)
    b.add_node("n", lambda s: {"messages": [AIMessage(content="hi")]})
    b.add_edge(START, "n")
    b.add_edge("n", END)
    return b.compile()


def _frames(graph, commands: list[dict]) -> list[dict]:
    out = io.StringIO()
    run(graph, io.StringIO("".join(json.dumps(c) + "\n" for c in commands)), out)
    return [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]


def _assert_handshake(frame: dict) -> None:
    assert frame["type"] == "ready", frame
    assert frame["version"] == langstage_vscode.__version__
    assert isinstance(frame["version"], str) and frame["version"]
    assert frame["protocol"] == 1
    assert frame["capabilities"] == EXPECTED_CAPABILITIES


def test_the_constants():
    assert PROTOCOL_VERSION == 1
    assert list(CAPABILITIES) == EXPECTED_CAPABILITIES


def test_ready_carries_the_handshake_for_a_working_agent():
    frames = _frames(_graph(), [{"type": "shutdown"}])
    _assert_handshake(frames[0])
    # The gh #152 field is unchanged, next to the handshake.
    assert frames[0]["checkpointer"] == {"kind": "InMemorySaver", "durable": False}


def test_ready_carries_the_handshake_when_the_agent_cannot_be_built():
    frames = _frames(lambda: None, [{"type": "shutdown"}])
    _assert_handshake(frames[0])
    assert "checkpointer" not in frames[0]
    assert set(frames[0]) == {"type", "version", "protocol", "capabilities"}
    # Same order as before: `ready`, then the error, then nothing.
    assert [f["type"] for f in frames] == ["ready", "error"]
    assert "not a runnable graph" in frames[1]["error"]


def test_ready_is_still_the_first_frame_and_a_turn_follows_as_before():
    frames = _frames(_graph(), [
        {"type": "message", "session_id": "s", "content": "hello"},
        {"type": "shutdown"},
    ])
    types = [f["type"] for f in frames]
    assert types[0] == "ready" and types.count("ready") == 1
    assert types[1] == "ack" and types[-1] == "turn_end"


def test_every_capability_names_something_real():
    # A new capability has to be classified here, with a check that it is real.
    assert set(CAPABILITIES) == COMMAND_CAPABILITIES | READY_FIELD_CAPABILITIES

    # Each command capability is a type the loop handles: it never gets the "unknown
    # command type" error (a control that it does for a made-up type is below).
    for cap in sorted(COMMAND_CAPABILITIES):
        frames = _frames(_graph(), [{"type": cap, "session_id": "s"}, {"type": "shutdown"}])
        errors = [f.get("error", "") for f in frames if f["type"] == "error"]
        assert not any("unknown command type" in e for e in errors), (cap, errors)

    frames = _frames(_graph(), [{"type": "no_such_command", "session_id": "s"}])
    assert any("unknown command type" in f.get("error", "") for f in frames), frames

    # `shutdown` ends the loop: a message after it never runs.
    frames = _frames(_graph(), [
        {"type": "shutdown"},
        {"type": "message", "session_id": "s", "content": "never"},
    ])
    assert [f["type"] for f in frames] == ["ready"]

    # Each ready-field capability is in the `ready` of a served agent.
    ready = _frames(_graph(), [{"type": "shutdown"}])[0]
    for cap in READY_FIELD_CAPABILITIES:
        assert cap in ready, cap


def test_selfcheck_json_carries_the_handshake(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--selfcheck", "--json"]) == 0
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["type"] == "selfcheck" and verdict["ok"] is True
    assert verdict["version"] == langstage_vscode.__version__
    assert verdict["protocol"] == 1
    assert verdict["capabilities"] == EXPECTED_CAPABILITIES


def test_a_failed_selfcheck_json_carries_the_handshake_too(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--agent", "no_such_module_gh89:graph", "--selfcheck", "--json"]) == 1
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["ok"] is False
    assert verdict["version"] == langstage_vscode.__version__
    assert verdict["protocol"] == 1
    assert verdict["capabilities"] == EXPECTED_CAPABILITIES


def test_the_real_sidecar_process_sends_the_handshake_first(tmp_path):
    # `python -m langstage_vscode --demo`, spawned the way the extension spawns it.
    env = dict(os.environ)
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT"):
        env.pop(var, None)
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--demo"],
        input=b'{"type": "shutdown"}\n', capture_output=True, cwd=str(tmp_path), env=env,
        timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    first = json.loads(proc.stdout.decode().splitlines()[0])
    _assert_handshake(first)
    assert first["checkpointer"] == {"kind": "InMemorySaver", "durable": False}


def test_the_readme_documents_the_handshake():
    text = README.read_text(encoding="utf-8")
    assert '"protocol": 1' in text
    assert '"capabilities": ["message", "decision", "cancel", "shutdown", "checkpointer"]' in text
    for cap in EXPECTED_CAPABILITIES:
        assert f"`{cap}`" in text, cap
