"""Regression tests for Wave 4 (advertised-not-honored + docs).

- gh #95: ``--show-config`` hid ``debug`` although the sidecar honors it.
- gh #98: the README advertises todo updates, but ``TodoExtractor`` was wired only for
  ``--demo=tools``, so a real agent's ``write_todos`` never produced a ``todos``
  ``extraction`` frame for the extension's Tasks checklist.
- gh #102: the README's Events list omitted frames the sidecar emits.
- gh #130: ``--selfcheck`` reported an interrupt-paused agent as healthy.
"""

import json
import re
from pathlib import Path

from langstage_vscode.sidecar import main

README = Path(__file__).resolve().parent.parent / "README.md"

_TODO_AGENT = """
import json
from langgraph.graph import StateGraph, START, END, MessagesState
from langchain_core.messages import AIMessage, ToolMessage
T = [{"content": "Read repo", "status": "completed"},
     {"content": "Write fix", "status": "in_progress"}]
def plan(state):
    return {"messages": [
        AIMessage(content="", tool_calls=[{"name": "write_todos", "args": {"todos": T}, "id": "tc1"}]),
        ToolMessage(content="Updated todo list to " + json.dumps(T), name="write_todos",
                    tool_call_id="tc1"),
        AIMessage(content="Updated the todos."),
    ]}
_b = StateGraph(MessagesState); _b.add_node("plan", plan)
_b.add_edge(START, "plan"); _b.add_edge("plan", END)
graph = _b.compile()
"""

_INTERRUPT_AGENT = """
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.types import interrupt
from langchain_core.messages import AIMessage
def ask(state: MessagesState):
    d = interrupt({"action_request": {"action": "confirm"}, "description": "approve?"})
    return {"messages": [AIMessage(content=f"resumed: {d}")]}
b = StateGraph(MessagesState); b.add_node("ask", ask)
b.add_edge(START, "ask"); b.add_edge("ask", END)
graph = b.compile()
"""


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    gh = tmp_path / "_global"
    gh.mkdir(exist_ok=True)
    monkeypatch.setenv("LANGSTAGE_CONFIG_HOME", str(gh))
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT", "LANGSTAGE_DEBUG", "DEEPAGENT_DEBUG",
                "DEEPAGENTS_CONFIG_HOME"):
        # setenv first so monkeypatch restores the var even though main() sets it
        # itself (it publishes LANGSTAGE_DEBUG when debug is on).
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)


def _frames(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# ── gh #95 ───────────────────────────────────────────────────────────────────


def test_show_config_reports_debug_text_and_json(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "langstage.toml").write_text('debug = true\n\n[agent]\nspec = "x.py:graph"\n')
    assert main(["--show-config"]) == 0
    text = capsys.readouterr().out
    assert re.search(r"debug\s*=\s*True\s*\[toml", text), text
    assert main(["--show-config", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["debug"]["value"] is True, payload
    # host / port / title stay hidden: the stdio sidecar really ignores them.
    for key in ("host", "port", "title"):
        assert key not in payload["config"], payload


def test_show_config_reports_debug_from_env(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_DEBUG", "1")
    assert main(["--show-config", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["debug"]["value"] is True
    assert payload["config"]["debug"]["source"].startswith("env"), payload


# ── gh #98 ───────────────────────────────────────────────────────────────────


def test_real_agent_write_todos_emits_a_todos_extraction(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "todo_agent_98.py").write_text(_TODO_AGENT)
    rc = main(["--agent", "todo_agent_98.py:graph", "--message", "plan it", "--json"])
    assert rc == 0
    frames = _frames(capsys.readouterr().out)
    ext = [f for f in frames if f.get("type") == "extraction"]
    assert ext, frames
    assert ext[0]["extracted_type"] == "todos" and ext[0]["tool_name"] == "write_todos"
    assert [t["content"] for t in ext[0]["data"]] == ["Read repo", "Write fix"]


def test_demo_tools_still_emits_its_own_extraction(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    assert main(["--demo=tools", "--message", "please use a tool", "--json"]) == 0
    frames = _frames(capsys.readouterr().out)
    assert any(f.get("extracted_type") == "demo_fact" for f in frames), frames


# ── gh #102 ──────────────────────────────────────────────────────────────────


def _events_block() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("**Events** (sidecar")
    block = text[start:]
    block = block[block.index("```jsonc") : ]
    return block[: block.index("```", len("```jsonc"))]


def test_readme_events_list_names_every_frame_the_sidecar_emits(monkeypatch, tmp_path, capsys):
    block = _events_block()
    documented = set(re.findall(r'"type":\s*"([a-z_]+)"', block))
    _isolate(monkeypatch, tmp_path)
    seen: set[str] = set()
    for msg in ("think about it", "please use a tool", "ask me first", "hello"):
        main(["--demo=tools", "--message", msg, "--json"])
        seen |= {f["type"] for f in _frames(capsys.readouterr().out)}
    protocol = {"cancelled", "error", "turn_end", "ack", "ready"}
    assert seen | protocol <= documented, sorted((seen | protocol) - documented)
    # The additive keys a client relies on are documented too.
    for key in ('"message_id"', '"outcome"', '"extracted_type"', '"allowed_decisions"'):
        assert key in block, key


# ── gh #130 ──────────────────────────────────────────────────────────────────


def test_selfcheck_reports_an_interrupt_pause_distinctly(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "always_interrupt_130.py").write_text(_INTERRUPT_AGENT)
    rc = main(["--agent", "always_interrupt_130.py:graph", "--selfcheck", "--json"])
    verdict = json.loads(capsys.readouterr().out.strip())
    assert rc == 2, verdict  # the same "paused awaiting a decision" code as --message
    assert verdict["ok"] is False and verdict["interrupt"] is True, verdict
    assert "interrupt" in verdict["message"], verdict


def test_selfcheck_interrupt_text_mode(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "always_interrupt_130b.py").write_text(_INTERRUPT_AGENT)
    rc = main(["--agent", "always_interrupt_130b.py:graph", "--selfcheck"])
    err = capsys.readouterr().err
    assert rc == 2
    assert err.startswith("PAUSED: "), err


def test_selfcheck_healthy_agent_is_still_ok(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    assert main(["--demo", "--selfcheck", "--json"]) == 0
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["ok"] is True and "interrupt" not in verdict
