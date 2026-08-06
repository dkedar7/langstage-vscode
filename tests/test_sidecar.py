"""Tests for the stdio sidecar command/event loop.

Since core 1.0 (ADR 0003) the sidecar streams turns ONLY through the in-process
AG-UI adapter, which drives a real compiled LangGraph agent — so these use real
graphs (the demo stub, or small compiled graphs), not a hand-rolled fake. The
streaming-frame behavior (content/tool/interrupt shapes) is covered in
tests/test_agui_sidecar.py; this file covers the command loop, validation, and
main()'s config resolution.
"""
import io
import json

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from langstage_core import load_agent_spec

from langstage_vscode.sidecar import main, run


# ── Fixtures ─────────────────────────────────────────────────────────


def _stub():
    """The keyless demo echo agent — a real CompiledGraph the AG-UI path accepts."""
    return load_agent_spec("langstage_core.demo.stub:graph")


def _boom_graph():
    """A compiled graph whose only node raises, to exercise error surfacing."""
    def boom(state):
        raise RuntimeError("kaboom")

    b = StateGraph(MessagesState)
    b.add_node("boom", boom)
    b.add_edge(START, "boom")
    b.add_edge("boom", END)
    return b.compile()


def _memory_graph():
    """A compiled graph WITH an in-process ``MemorySaver`` — the canonical
    ``graph.compile(checkpointer=...)`` the README's memory note points at. Its one
    node reports how many messages it has seen on the thread, so a rising count is
    direct proof the checkpointer survived across turns.
    """
    def respond(state):
        n = len(state["messages"])  # includes this turn's human message
        return {"messages": [AIMessage(content=f"seen {n}")]}

    b = StateGraph(MessagesState)
    b.add_node("respond", respond)
    b.add_edge(START, "respond")
    b.add_edge("respond", END)
    return b.compile(checkpointer=MemorySaver())


def _interrupt_graph():
    """A checkpointer-backed HITL agent that pauses on ``interrupt(...)`` — the issue's
    minimal repro (gh #58). The single-dict payload reaches the wire as the NESTED
    ``action_request`` shape (``{"action_request": {"action": ...}, ...}``), the shape
    a real ``interrupt({...})`` HITL agent emits."""
    def ask(state):
        answer = interrupt({"action_request": {"action": "confirm", "args": {}},
                            "description": "approve?"})
        return {"messages": [AIMessage(content=f"resumed with: {answer}")]}

    b = StateGraph(MessagesState)
    b.add_node("ask", ask)
    b.add_edge(START, "ask")
    b.add_edge("ask", END)
    return b.compile(checkpointer=MemorySaver())


def drive(graph, commands):
    """Feed JSON commands through run() and return parsed event dicts."""
    stdin = io.StringIO("".join(json.dumps(c) + "\n" for c in commands))
    stdout = io.StringIO()
    run(graph, stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


# ── Tests ────────────────────────────────────────────────────────────


def test_version_flag(capsys):
    """--version prints the sidecar version and exits 0 (gh #-dogfood)."""
    import pytest

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "langstage-vscode-sidecar" in out


def test_help_output_is_ascii(capsys):
    """--help output must be ASCII — an em-dash in a help string mojibakes on a
    cp1252 console (gh #-dogfood)."""
    import pytest

    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "--demo" in out
    assert out.isascii(), "sidecar --help must be ASCII-safe"


def test_ready_is_first_event():
    events = drive(_stub(), [{"type": "shutdown"}])
    assert events[0] == {"type": "ready"}


def test_message_turn_emits_content_and_terminals():
    events = drive(_stub(), [
        {"type": "message", "session_id": "s", "content": "hi there"},
        {"type": "shutdown"},
    ])
    types = [e["type"] for e in events]
    assert types[0] == "ready"
    assert {"type": "ack", "ref": "message"} in events
    assert "content" in types
    assert "complete" in types
    assert any(e["type"] == "turn_end" and e["session_id"] == "s" for e in events)
    content = "".join(e.get("content", "") for e in events if e["type"] == "content")
    assert "hi there" in content


# ── gh #75: a finished multi-text-block AIMessage keeps ALL its text blocks ───


def _multi_block_graph():
    """A compiled graph whose single node returns a FINISHED (non-token-streamed)
    ``AIMessage`` whose ``content`` is a list of two ``text`` blocks — the gh #75 repro.

    Before core 1.0.30 the snapshot walk relied on ag-ui's ``resolve_message_content``,
    which flattens list content to the FIRST text block, so the second block was
    silently dropped (wrong-but-plausible output, exit 0). Core 1.0.30 re-reads the
    original LangChain messages from the checkpoint and emits ``message.text`` (all
    text blocks joined), so BOTH blocks now reach the wire.
    """
    def respond(state):
        return {"messages": [AIMessage(content=[
            {"type": "text", "text": "First half of the answer. "},
            {"type": "text", "text": "Second half of the answer."},
        ])]}

    b = StateGraph(MessagesState)
    b.add_node("respond", respond)
    b.add_edge(START, "respond")
    b.add_edge("respond", END)
    return b.compile()


def test_finished_multi_block_aimessage_keeps_all_blocks():
    # gh #75: a keyless user's finished AIMessage with 2+ text blocks must emit EVERY
    # block, not just the first. Requires langstage-core >=1.0.30 (floor raised in
    # pyproject.toml); the fix is inherited from core, this guards the sidecar against
    # a regression on either wire.
    events = drive(_multi_block_graph(), [
        {"type": "message", "session_id": "s", "content": "hi"},
        {"type": "shutdown"},
    ])
    content = "".join(e.get("content", "") for e in events if e["type"] == "content")
    assert "First half of the answer. " in content, content
    assert "Second half of the answer." in content, content  # dropped before core 1.0.30


# ── gh #54: conversational memory across turns in one persistent process ─────


def _turns(events):
    """Split a run()'s event stream into per-turn lists at each ``turn_end``."""
    turns, cur = [], []
    for e in events:
        cur.append(e)
        if e["type"] == "turn_end":
            turns.append(cur)
            cur = []
    return turns


def _reply(turn):
    return "".join(e.get("content", "") for e in turn if e["type"] == "content")


def test_memory_persists_across_turns_in_one_process():
    # gh #54: a checkpointer-backed agent must remember prior turns when the sidecar
    # PROCESS is persistent across turns (one run() loop, same session_id -> same
    # thread_id). This is the invariant the VS Code extension now relies on by keeping
    # ONE sidecar alive per conversation instead of spawning a fresh process — and a
    # fresh in-process MemorySaver — per message. Turn 2 must see turn 1's human+ai
    # messages plus its own human message (3), not start over at 1.
    events = drive(_memory_graph(), [
        {"type": "message", "session_id": "vscode", "content": "first"},
        {"type": "message", "session_id": "vscode", "content": "second"},
        {"type": "shutdown"},
    ])
    turns = _turns(events)
    assert len(turns) == 2, [e["type"] for e in events]
    assert _reply(turns[0]) == "seen 1"  # turn 1 sees only its own human message
    assert _reply(turns[1]) == "seen 3"  # turn 2 remembers turn 1 (human+ai+human)


def test_memory_lost_when_each_turn_gets_a_fresh_checkpointer():
    # gh #54, the bug's shape: the extension USED to spawn a brand-new sidecar process —
    # and thus a brand-new in-process MemorySaver — for every message, so turn 2 forgot
    # turn 1. A distinct graph instance per turn models that fresh process: each sees
    # only its own single human message ("seen 1"), never "seen 3". This is exactly why
    # the extension must keep ONE process alive across a conversation's turns.
    for content in ("first", "second"):
        events = drive(_memory_graph(), [
            {"type": "message", "session_id": "vscode", "content": content},
            {"type": "shutdown"},
        ])
        assert _reply(_turns(events)[0]) == "seen 1"


def test_invalid_json_reported():
    stdin = io.StringIO("not json\n")
    stdout = io.StringIO()
    run(_stub(), stdin, stdout)
    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert any(e["type"] == "error" and "invalid JSON" in e["error"] for e in events)


def test_unknown_command_reported():
    events = drive(_stub(), [{"type": "nope"}, {"type": "shutdown"}])
    assert any(e["type"] == "error" and "unknown command" in e["error"] for e in events)


def test_empty_message_reported():
    events = drive(_stub(), [
        {"type": "message", "content": ""},
        {"type": "shutdown"},
    ])
    assert any(e["type"] == "error" and "content" in e["error"] for e in events)


def test_decision_without_list_reported():
    events = drive(_stub(), [{"type": "decision"}, {"type": "shutdown"}])
    assert any(e["type"] == "error" and "decisions" in e["error"] for e in events)


def test_empty_decision_list_reported():
    # gh #33: an empty `decisions: []` has no interrupt to resume, so it must emit an
    # `error` frame like the empty-`message` path — not ack + drive a spurious turn.
    events = drive(_stub(), [{"type": "decision", "decisions": []}, {"type": "shutdown"}])
    assert any(e["type"] == "error" and "decisions" in e["error"] for e in events)
    # And it must NOT ack or run a turn.
    assert not any(e.get("type") == "ack" and e.get("ref") == "decision" for e in events)
    assert not any(e.get("type") == "turn_end" for e in events)


def test_graph_error_surfaced():
    """A graph that raises mid-turn surfaces an error frame, not a crash."""
    events = drive(_boom_graph(), [
        {"type": "message", "content": "go"},
        {"type": "shutdown"},
    ])
    err = [e for e in events if e["type"] == "error"]
    assert err and "kaboom" in err[-1]["error"]


def _factory_fn():
    """A FACTORY function, not a compiled graph — the gh #46 footgun (spec points at
    the builder instead of the compiled `module:graph` attribute)."""
    def make_graph():
        b = StateGraph(MessagesState)
        b.add_node("n", lambda s: s)
        b.add_edge(START, "n")
        b.add_edge("n", END)
        return b.compile()

    return make_graph


def test_non_graph_spec_emits_error_frame_not_crash():
    # gh #46: a spec that loads a non-runnable object (a factory fn, a bare value) used
    # to crash the sidecar with a raw AttributeError right after `ready`, with nothing on
    # the protocol stream. It must degrade to an actionable `error` frame instead.
    events = drive(_factory_fn(), [{"type": "message", "content": "hi"}, {"type": "shutdown"}])
    assert events[0] == {"type": "ready"}  # ready still emitted first
    err = [e for e in events if e["type"] == "error"]
    assert err, "expected an error frame for a non-runnable graph, not a crash"
    assert "not a runnable graph" in err[-1]["error"]
    assert "function" in err[-1]["error"]  # names what it actually loaded


def test_non_graph_spec_error_names_the_spec():
    # On the real runtime path run() gets the spec, so the message names it — matching
    # the actionable message --selfcheck gives (shared via _runnable_graph_error).
    stdin = io.StringIO(json.dumps({"type": "shutdown"}) + "\n")
    stdout = io.StringIO()
    run(42, stdin, stdout, spec="./x.py:make_graph")  # a bare int is not a graph
    events = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    err = [e for e in events if e["type"] == "error"]
    assert err and "./x.py:make_graph" in err[-1]["error"]
    assert "int" in err[-1]["error"]


# ── main(): config resolution + --demo / --show-config ───────────────


def _isolate_config(monkeypatch, tmp_path):
    """Point cwd + the global config home at an empty tmp dir and strip
    legacy + canonical env so main() resolves from pure defaults."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGSTAGE_CONFIG_HOME", str(tmp_path))
    # LANGSTAGE_DEBUG is in the strip list so the default (no-debug) tests start clean AND
    # so monkeypatch restores it at teardown even though main() sets os.environ directly on
    # --traceback (gh #83) — keeping a --traceback test from leaking debug into later tests.
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT", "LANGSTAGE_DEBUG"):
        monkeypatch.delenv(var, raising=False)


def test_main_show_config(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--show-config"]) == 0
    assert "LANGSTAGE_AGENT_SPEC" in capsys.readouterr().out


def test_show_config_survives_cp1252_stdout_with_non_latin1_value(tmp_path):
    # gh #42: on a cp1252 (strict) stdout — a Western-Windows console, or the pipe the
    # VS Code extension spawns the sidecar on — a resolved value with a non-Latin-1 char
    # made `print(cfg.describe(...))` crash with UnicodeEncodeError and emit nothing.
    # Driven as a subprocess with PYTHONIOENCODING=cp1252 (mirrors that stdout).
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    env["LANGSTAGE_AGENT_SPEC"] = "app_日本.py:graph"  # CJK -> not cp1252-encodable
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)  # no stray real config
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--show-config"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "UnicodeEncodeError" not in proc.stderr
    # the report is emitted; the unrepresentable chars degrade to escapes, not a crash
    assert "agent_spec" in proc.stdout
    assert "\\u65e5" in proc.stdout


def test_main_accepts_short_agent_flag(monkeypatch, tmp_path, capsys):
    # gh dogfood-F9: cli uses `-a`; the sidecar accepts it too (was --agent only), so
    # the same spec + flag work across surfaces.
    _isolate_config(monkeypatch, tmp_path)
    assert main(["-a", "langstage_core.demo.stub:graph", "--show-config"]) == 0
    assert "langstage_core.demo.stub:graph" in capsys.readouterr().out


def test_main_accepts_and_ignores_removed_agui_flag(monkeypatch, tmp_path, capsys):
    # gh #38: --agui was removed in 0.5.0 (AG-UI is the only path), but the 0.5.0
    # CHANGELOG promised it stays accepted-and-ignored for a transition window so old
    # launch configs don't crash. It must NOT raise argparse "unrecognized arguments"
    # (exit 2) — the sidecar proceeds exactly as if the flag weren't there.
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--agui", "-a", "langstage_core.demo.stub:graph", "--show-config"]) == 0
    assert "langstage_core.demo.stub:graph" in capsys.readouterr().out


def test_main_show_config_omits_inert_server_keys(monkeypatch, tmp_path, capsys):
    # The stdio sidecar never opens a socket or renders a UI, so host/port/debug/
    # title do nothing — --show-config must not advertise them. (gh #14)
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_PORT", "12345")
    monkeypatch.setenv("LANGSTAGE_HOST", "0.0.0.0")
    assert main(["--show-config"]) == 0
    out = capsys.readouterr().out
    for inert in ("LANGSTAGE_PORT", "LANGSTAGE_HOST", "LANGSTAGE_DEBUG", "LANGSTAGE_TITLE"):
        assert inert not in out
    assert "\n  port " not in out and "\n  host " not in out
    # ...but the keys the sidecar honors are still shown.
    assert "agent_spec" in out and "workspace_root" in out


# ── gh #71: --show-config --json — the machine-readable form ───────────


def test_show_config_json_emits_machine_readable_object(monkeypatch, tmp_path, capsys):
    # gh #71: --show-config --json used to be silently accepted and IGNORED — it printed
    # the aligned human text with exit 0, so a caller expecting JSON got text and no
    # error. Now it emits one JSON object (nothing else on stdout, exit 0): the resolved
    # values + provenance, with a "type" discriminator matching the sibling
    # --selfcheck --json envelope ({"type": "selfcheck", ...}).
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--show-config", "--json"]) == 0
    out = capsys.readouterr().out
    obj = json.loads(out)  # a single, valid JSON object — nothing else on stdout
    assert isinstance(obj, dict)
    assert obj["type"] == "show_config"
    # Provenance for the two keys the sidecar honors.
    assert set(obj["config"]) == {"agent_spec", "workspace_root"}
    for entry in obj["config"].values():
        assert {"value", "source", "env", "legacy_env", "toml"} <= set(entry)
    # Pure defaults: no toml, values at their defaults.
    assert obj["config"]["agent_spec"]["value"] is None
    assert obj["config"]["agent_spec"]["source"] == "default"
    assert obj["config"]["workspace_root"]["value"] == "."
    # core >=1.0.32 adds `unknown_keys` to the toml block (gh #82); no toml here -> empty.
    assert obj["toml"] == {"found": False, "path": None, "unknown_keys": []}


def test_show_config_json_matches_config_dict(monkeypatch, tmp_path, capsys):
    # "JSON sources match the config": the emitted config/toml are exactly
    # config_dict() (core's machine-readable twin of describe()), with the SAME omit
    # list — so the JSON [source] labels are the resolver's own, not a re-derivation
    # that could drift from the human text. An env-sourced value proves the source
    # tracks the layer it actually came from (gh #71).
    from langstage_core.host import HostConfig

    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_AGENT_SPEC", "env_mod:graph")
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    expected = json.loads(
        json.dumps(
            HostConfig.resolve(
                overrides={"agent_spec": None, "workspace_root": None}
            ).config_dict(omit_keys=["host", "port", "debug", "title"]),
            default=str,
        )
    )
    assert obj["config"] == expected["config"]
    assert obj["toml"] == expected["toml"]
    # The env-sourced value is reported as coming from the env layer, not default.
    assert obj["config"]["agent_spec"]["value"] == "env_mod:graph"
    assert obj["config"]["agent_spec"]["source"].startswith("env")


def test_show_config_json_provenance_matches_toml(monkeypatch, tmp_path, capsys):
    # A langstage.toml [agent].spec shows up in the JSON as its value, a toml-layer
    # source, and the resolved toml path — the same resolution the human text reports
    # ("TOML read from: ..."). Provenance the extension can assert on (gh #71).
    _isolate_config(monkeypatch, tmp_path)
    (tmp_path / "langstage.toml").write_text('[agent]\nspec = "mymod:graph"\n')
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    spec = obj["config"]["agent_spec"]
    assert spec["value"] == "mymod:graph"
    assert "toml" in spec["source"]  # e.g. "toml (langstage.toml)"
    assert obj["toml"]["found"] is True
    assert obj["toml"]["path"] is not None
    assert obj["toml"]["path"].endswith("langstage.toml")


def test_show_config_json_omits_inert_server_keys(monkeypatch, tmp_path, capsys):
    # The host/port/debug/title omit list applies to the JSON form too, so the text
    # and JSON agree on which keys show — the inert server/UI keys appear in neither
    # (gh #14/#71).
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_PORT", "12345")
    monkeypatch.setenv("LANGSTAGE_HOST", "0.0.0.0")
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert set(obj["config"]) == {"agent_spec", "workspace_root"}
    for inert in ("host", "port", "debug", "title"):
        assert inert not in obj["config"]


def test_show_config_without_json_stays_human_text(monkeypatch, tmp_path, capsys):
    # The regression guard for the non-json path: --show-config ALONE stays the aligned
    # human report, not JSON — unchanged by gh #71 (which only added the --json branch).
    import pytest as _pytest

    _isolate_config(monkeypatch, tmp_path)
    assert main(["--show-config"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Resolved config")
    with _pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_main_no_spec_emits_error(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main([]) == 1
    err = json.loads(capsys.readouterr().out.strip())
    assert err["type"] == "error"
    assert "langstage.toml" in err["error"]


def test_main_demo_conflicts_with_agent(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--agent", "x.py:g"]) == 1
    err = json.loads(capsys.readouterr().out.strip())
    assert "mutually exclusive" in err["error"]


def test_main_toml_supplies_agent_spec(monkeypatch, tmp_path, capsys):
    """The langstage.toml resolver is actually wired in: [agent].spec from a
    project file reaches load_agent_spec with no flags or env."""
    _isolate_config(monkeypatch, tmp_path)
    (tmp_path / "langstage.toml").write_text(
        '[agent]\nspec = "langstage_core.demo.stub:graph"\n'
    )
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"type": "shutdown"}) + "\n")
    )
    assert main([]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert events[0] == {"type": "ready"}


def test_main_legacy_toml_still_works(monkeypatch, tmp_path, capsys):
    """Pre-rename deepagents.toml keeps resolving as a deprecated fallback."""
    _isolate_config(monkeypatch, tmp_path)
    (tmp_path / "deepagents.toml").write_text(
        '[agent]\nspec = "langstage_core.demo.stub:graph"\n'
    )
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"type": "shutdown"}) + "\n")
    )
    assert main([]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert events[0] == {"type": "ready"}


def test_legacy_module_import_warns():
    import sys

    import pytest as _pytest

    sys.modules.pop("deepagent_vscode", None)
    sys.modules.pop("deepagent_vscode.sidecar", None)
    with _pytest.warns(DeprecationWarning, match="langstage_vscode"):
        import deepagent_vscode  # noqa: F401
    import deepagent_vscode.sidecar as old_sidecar
    import langstage_vscode.sidecar as new_sidecar

    assert old_sidecar is new_sidecar


def test_legacy_alias_exposes_full_public_api():
    # gh #17: the alias must re-export the old package's public API
    # (main, run, __version__) — not only the sidecar submodule.
    import sys
    import warnings

    sys.modules.pop("deepagent_vscode", None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import deepagent_vscode as alias
    import langstage_vscode as canonical

    assert alias.main is canonical.main
    assert alias.run is canonical.run
    assert alias.__version__ == canonical.__version__
    for name in ("main", "run", "sidecar", "__version__"):
        assert hasattr(alias, name), name


def test_main_demo_end_to_end(monkeypatch, tmp_path, capsys):
    """--demo answers a real message turn through the stub agent — no keys."""
    _isolate_config(monkeypatch, tmp_path)
    commands = (
        json.dumps({"type": "message", "session_id": "s", "content": "hi demo"})
        + "\n"
        + json.dumps({"type": "shutdown"})
        + "\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(commands))
    assert main(["--demo"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    types = [e["type"] for e in events]
    assert "ready" in types and "complete" in types and "turn_end" in types
    content = "".join(e.get("content", "") for e in events if e["type"] == "content")
    assert "hi demo" in content


# ── gh #73: --demo {echo,tools} choice (parity with langstage-agui --demo=tools) ──


def test_main_demo_tools_emits_tool_frames(monkeypatch, tmp_path, capsys):
    """--demo=tools serves the rich-frame demo, so a keyless one-shot on the
    'use a tool' trigger emits the tool_start/tool_end frames the echo stub never
    produces — the whole point of the choice (gh #73)."""
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo=tools", "--message", "please use a tool", "--json"]) == 0
    frames = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    types = {f.get("type") for f in frames}
    assert "tool_start" in types
    assert "tool_end" in types
    assert "complete" in types


def test_main_demo_tools_space_form_also_works(monkeypatch, tmp_path, capsys):
    """`--demo tools` (space form) resolves the same as `--demo=tools` (nargs='?')."""
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "tools", "--message", "please use a tool", "--json"]) == 0
    types = {
        json.loads(ln).get("type")
        for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    }
    assert "tool_start" in types and "tool_end" in types


def test_main_demo_tools_emits_reasoning_frames(monkeypatch, tmp_path, capsys):
    """The 'think' trigger on the tools demo streams `reasoning` frames — another
    non-content frame type the echo stub can't reach (gh #73)."""
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo=tools", "--message", "think about it", "--json"]) == 0
    types = {
        json.loads(ln).get("type")
        for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    }
    assert "reasoning" in types


def test_main_demo_tools_interrupt_pauses_with_exit_2(monkeypatch, tmp_path, capsys):
    """The 'ask me' trigger raises a HITL interrupt: the one-shot emits an `interrupt`
    frame and exits 2 (paused awaiting a decision), keyless (gh #73)."""
    _isolate_config(monkeypatch, tmp_path)
    rc = main(["--demo=tools", "--message", "ask me first", "--json"])
    assert rc == 2
    types = {
        json.loads(ln).get("type")
        for ln in capsys.readouterr().out.splitlines()
        if ln.strip()
    }
    assert "interrupt" in types


def test_main_demo_bare_still_echoes_no_tool_frames(monkeypatch, tmp_path, capsys):
    """Bare --demo is unchanged: the echo stub, which only ever emits `content`
    frames and echoes the prompt verbatim — never the rich tool_start/tool_end that
    --demo=tools adds (backward compatibility, gh #73)."""
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--message", "please use a tool", "--json"]) == 0
    frames = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    types = {f.get("type") for f in frames}
    assert "content" in types
    assert "tool_start" not in types and "tool_end" not in types
    text = "".join(f.get("content", "") for f in frames if f.get("type") == "content")
    assert "please use a tool" in text  # echoed back, not consumed by a tool call


def test_main_demo_echo_explicit_matches_bare(monkeypatch, tmp_path, capsys):
    """--demo=echo explicitly selects the echo stub — identical to bare --demo."""
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo=echo", "--message", "hello there"]) == 0
    assert "You said: hello there" in capsys.readouterr().out


def test_main_demo_invalid_value_is_argparse_error(monkeypatch, tmp_path, capsys):
    """--demo=<bad> is an argparse choices error (exit 2), not a silent fallback (gh #73)."""
    import pytest

    _isolate_config(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--demo=bogus"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_main_demo_tools_conflicts_with_agent(monkeypatch, tmp_path, capsys):
    """The mutual-exclusion guard holds for the tools variant too (gh #73)."""
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo=tools", "--agent", "x.py:g"]) == 1
    err = json.loads(capsys.readouterr().out.strip())
    assert "mutually exclusive" in err["error"]


def test_selfcheck_demo_tools_is_healthy(monkeypatch, tmp_path, capsys):
    """--selfcheck --demo=tools validates the rich demo graph keyless (gh #73)."""
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo=tools", "--selfcheck"]) == 0
    assert capsys.readouterr().err.startswith("OK:")


# ── gh #19: --workspace override must reach the agent (os.environ), not just --show-config ──


def test_workspace_override_reaches_agent_env(monkeypatch, tmp_path):
    """The agent reads LANGSTAGE_WORKSPACE_ROOT from os.environ. setdefault() was a
    no-op when the env var was already exported, so a --workspace override was
    silently dropped (agent saw the stale env value) even though --show-config
    reported the override as winning. (gh #19)"""
    import os
    from pathlib import Path

    _isolate_config(monkeypatch, tmp_path)
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    monkeypatch.setenv("LANGSTAGE_WORKSPACE_ROOT", str(env_dir))  # preset env
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"type": "shutdown"}) + "\n"))

    rc = main(["--agent", "langstage_core.demo.stub:graph", "--workspace", str(cli_dir)])
    assert rc == 0
    seen = Path(os.environ["LANGSTAGE_WORKSPACE_ROOT"]).resolve()
    assert seen == cli_dir.resolve(), f"override dropped: agent would read {seen}"
    assert seen != env_dir.resolve()
    # legacy name is kept in sync, not left stale
    assert Path(os.environ["DEEPAGENT_WORKSPACE_ROOT"]).resolve() == cli_dir.resolve()


def test_no_override_keeps_env_workspace(monkeypatch, tmp_path):
    """Regression: with no --workspace flag, the exported env value still wins."""
    import os
    from pathlib import Path

    _isolate_config(monkeypatch, tmp_path)
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("LANGSTAGE_WORKSPACE_ROOT", str(env_dir))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"type": "shutdown"}) + "\n"))

    rc = main(["--agent", "langstage_core.demo.stub:graph"])
    assert rc == 0
    assert Path(os.environ["LANGSTAGE_WORKSPACE_ROOT"]).resolve() == env_dir.resolve()


# ── gh #21: --selfcheck / --smoke preflight ──────────────────────────────────


def test_selfcheck_demo_is_healthy(monkeypatch, tmp_path, capsys):
    """--selfcheck with no agent validates the runtime via the demo stub."""
    _isolate_config(monkeypatch, tmp_path)
    rc = main(["--selfcheck"])
    assert rc == 0
    assert capsys.readouterr().err.startswith("OK:")


def test_selfcheck_non_runnable_agent_fails_precisely(monkeypatch, tmp_path, capsys):
    """A spec that loads but isn't a CompiledGraph fails with a precise message,
    not a cryptic first-message AttributeError."""
    _isolate_config(monkeypatch, tmp_path)
    rc = main(["--selfcheck", "--agent", "os:getcwd"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a runnable graph" in err
    assert "builtin_function_or_method" in err  # names what it actually loaded


def test_selfcheck_unloadable_agent_fails(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    rc = main(["--selfcheck", "--agent", "no_such_module:graph"])
    assert rc == 1
    assert "failed to load" in capsys.readouterr().err


def test_selfcheck_json_verdict(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    rc = main(["--smoke", "--json"])  # --smoke alias
    assert rc == 0
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["type"] == "selfcheck" and verdict["ok"] is True


def test_main_operates_from_workspace_cwd(monkeypatch, tmp_path):
    # ADR 0006: main() chdirs into the resolved workspace AFTER resolving the spec, so a
    # BYO agent's raw relative writes land in the workspace. (conftest restores cwd.)
    import os
    from pathlib import Path

    _isolate_config(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # empty -> run() loop exits at once
    rc = main(["--agent", "langstage_core.demo.stub:graph", "--workspace", str(ws)])
    assert rc == 0
    assert Path(os.getcwd()).resolve() == ws.resolve()


# ── one-shot --message (gh #48) ──────────────────────────────────────


def test_main_message_prints_assembled_reply(monkeypatch, tmp_path, capsys):
    # gh #48: --message drives ONE turn and prints the agent's reply, exit 0, with no
    # caller-crafted NDJSON + shutdown handshake.
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--message", "hello there"]) == 0
    out = capsys.readouterr().out
    assert "You said: hello there" in out
    assert '"type"' not in out  # human mode prints text, not raw protocol frames


def test_main_message_json_emits_raw_frames(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--message", "hi", "--json"]) == 0
    frames = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    types = [f.get("type") for f in frames]
    assert "content" in types and "complete" in types
    text = "".join(f.get("content", "") for f in frames if f.get("type") == "content")
    assert "hi" in text


# ── streaming --message (gh #50) ─────────────────────────────────────


def test_oneshot_sink_human_streams_content_incrementally():
    # gh #50: the sink must forward each `content` frame to stdout the instant it
    # arrives (not buffer the whole turn) — asserted by checking stdout already holds
    # the text BEFORE the terminal frames are fed.
    from langstage_vscode.sidecar import _OneShotSink

    out, err = io.StringIO(), io.StringIO()
    sink = _OneShotSink(out, err, as_json=False)

    sink.write(json.dumps({"type": "content", "content": "Hello "}) + "\n")
    assert out.getvalue() == "Hello "  # visible before any complete/turn_end
    sink.write(json.dumps({"type": "content", "content": "world"}) + "\n")
    assert out.getvalue() == "Hello world"
    sink.write(json.dumps({"type": "complete"}) + "\n")
    sink.write(json.dumps({"type": "turn_end", "session_id": "once"}) + "\n")
    sink.close_reply()

    assert out.getvalue() == "Hello world\n"  # trailing newline closes the live reply
    assert err.getvalue() == ""  # human mode: no protocol noise on stderr
    assert sink.saw_error is False


def test_oneshot_sink_json_streams_each_frame_verbatim():
    # gh #50: in --json mode every frame (error frames included) is forwarded to
    # stdout the instant it's emitted, as a genuine streaming NDJSON source.
    from langstage_vscode.sidecar import _OneShotSink

    out, err = io.StringIO(), io.StringIO()
    sink = _OneShotSink(out, err, as_json=True)

    sink.write(json.dumps({"type": "content", "content": "hi"}) + "\n")
    assert json.loads(out.getvalue().strip()) == {"type": "content", "content": "hi"}
    sink.write(json.dumps({"type": "error", "error": "boom"}) + "\n")

    frames = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    assert any(f.get("type") == "error" for f in frames)  # error rides the stdout stream
    assert sink.saw_error is True
    assert err.getvalue() == ""  # --json keeps everything on the one NDJSON channel


def test_oneshot_sink_human_routes_error_to_stderr():
    # Human mode keeps stdout the clean reply channel and surfaces the failure on
    # stderr, and still flags saw_error for the exit code — the gh #48 contract.
    from langstage_vscode.sidecar import _OneShotSink

    out, err = io.StringIO(), io.StringIO()
    sink = _OneShotSink(out, err, as_json=False)

    sink.write(json.dumps({"type": "error", "error": "kaboom"}) + "\n")
    sink.close_reply()

    assert out.getvalue() == ""  # nothing on stdout
    assert "error: kaboom" in err.getvalue()
    assert sink.saw_error is True


def test_oneshot_sink_reassembles_frame_split_across_writes():
    # Defensive: even if a frame were delivered in two write() calls, buffering on the
    # newline boundary must reassemble it rather than mis-parse a partial line.
    from langstage_vscode.sidecar import _OneShotSink

    out, err = io.StringIO(), io.StringIO()
    sink = _OneShotSink(out, err, as_json=False)
    frame = json.dumps({"type": "content", "content": "abc"})
    sink.write(frame[:5])
    assert out.getvalue() == ""  # partial line: nothing emitted yet
    sink.write(frame[5:] + "\n")
    assert out.getvalue() == "abc"


def test_main_message_streams_frame_by_frame_not_buffered(monkeypatch, tmp_path):
    # gh #50 end-to-end: --message must stream each content frame to the REAL stdout as
    # it arrives, not buffer the turn and dump it once. The demo stub emits its reply
    # across several content frames, so a streaming path writes it in multiple pieces,
    # whereas the old buffered path emitted the whole reply in a single write.
    _isolate_config(monkeypatch, tmp_path)

    class RecordingOut:
        encoding = "utf-8"

        def __init__(self):
            self.writes = []

        def write(self, s):
            self.writes.append(s)

        def flush(self):
            pass

    rec = RecordingOut()
    monkeypatch.setattr("sys.stdout", rec)
    assert main(["--demo", "--message", "hello there"]) == 0

    joined = "".join(rec.writes)
    content_writes = [w for w in rec.writes if w.strip()]
    assert len(content_writes) >= 2, rec.writes  # reply arrived in pieces, not one blob
    assert "hello there" in joined
    assert joined.endswith("\n")  # the live reply is closed off with a trailing newline


def test_message_human_survives_cp1252_stdout_with_non_latin1_reply(tmp_path):
    # gh #51: on a cp1252 (strict) stdout — a Western-Windows console, or the pipe the
    # VS Code extension spawns the sidecar on — a reply with a non-Latin-1 char (an
    # emoji/CJK char an LLM emits routinely) made the one-shot --message `print(reply)`
    # crash with UnicodeEncodeError and emit nothing (gh #42's --show-config fix was
    # never applied to this newer path). Driven as a subprocess with
    # PYTHONIOENCODING=cp1252, mirroring the #42 test. The demo stub echoes the prompt,
    # so a ✅ in the prompt rides straight into the assembled reply.
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)  # no stray real config
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--demo", "--message", "build ✅ done"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "UnicodeEncodeError" not in proc.stderr
    # the reply is emitted; the unrepresentable char degrades to an escape, not a crash
    assert "You said: build" in proc.stdout
    assert "\\u2705" in proc.stdout


def test_main_message_nonrunnable_spec_exits_1_with_clean_stdout(monkeypatch, tmp_path, capsys):
    # A loads-but-not-runnable spec: exit 1, the actionable #46 message on stderr, and
    # stdout stays the clean reply channel (empty) — reuses run()'s runnable guard.
    _isolate_config(monkeypatch, tmp_path)
    agent = tmp_path / "factory.py"
    agent.write_text("def make_graph():\n    pass\n")
    monkeypatch.setenv("LANGSTAGE_AGENT_SPEC", f"{agent}:make_graph")
    assert main(["--message", "hi"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "not a runnable graph" in captured.err


# ── interactive --repl (gh #56) ──────────────────────────────────────
#
# --repl is the multi-turn companion to one-shot --message: it reads a prompt per
# input line, drives a turn over ONE long-lived session, and loops until EOF/:quit.
# Because every turn shares one session_id (-> one thread_id) in one persistent
# run() loop, a checkpointer-backed agent remembers prior turns — the whole point
# (verify the README's conversational-memory caveat from the CLI).


def drive_repl(graph, lines, *, as_json=False, spec=None):
    """Drive _run_repl with a scripted stdin (one REPL input per line) and return
    (exit_code, stdout, stderr). Prompt drawing is off (piped stdin, not a TTY)."""
    from langstage_vscode.sidecar import _run_repl

    stdin = io.StringIO("".join(line + "\n" for line in lines))
    out, err = io.StringIO(), io.StringIO()
    rc = _run_repl(
        graph,
        spec=spec,
        as_json=as_json,
        stdin=stdin,
        stdout=out,
        stderr=err,
        show_prompt=False,
    )
    return rc, out.getvalue(), err.getvalue()


def test_repl_memory_persists_across_turns():
    # The centerpiece: two turns over one --repl session against a checkpointer-backed
    # agent. Turn 2 must SEE turn 1 (human+ai+human = 3 messages), proving the session
    # (thread_id) survived across turns — exactly what --message (fresh session_id
    # "once" per call) cannot show. Driven in --json so each turn's reply is parseable.
    rc, out, _ = drive_repl(_memory_graph(), ["first", "second"], as_json=True)
    assert rc == 0
    turns = _turns([json.loads(ln) for ln in out.splitlines() if ln.strip()])
    assert len(turns) == 2
    assert _reply(turns[0]) == "seen 1"  # turn 1 sees only its own human message
    assert _reply(turns[1]) == "seen 3"  # turn 2 remembers turn 1 (human+ai+human)


def test_repl_human_mode_renders_each_reply_on_its_own_line():
    # Human mode: each turn's assembled reply is printed and closed off with a newline
    # at turn_end, so replies don't run together and stdout stays the clean reply
    # channel (no raw protocol frames). Ends on EOF (the scripted stdin runs out).
    rc, out, err = drive_repl(_memory_graph(), ["first", "second"])
    assert rc == 0
    assert out == "seen 1\nseen 3\n"  # per-turn reply, each terminated by turn_end
    assert '"type"' not in out  # human mode prints text, not protocol frames
    assert err == ""  # no prompt (show_prompt off) and no error noise


def test_repl_exits_cleanly_on_quit_and_ignores_later_lines():
    # A ':quit' line ends the session immediately; any line after it is never read,
    # so it never drives a turn. Exit is clean (0).
    rc, out, _ = drive_repl(_stub(), ["hi there", ":quit", "unreached"], as_json=True)
    assert rc == 0
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert len(_turns(frames)) == 1  # only the pre-:quit line drove a turn
    content = "".join(f.get("content", "") for f in frames if f["type"] == "content")
    assert "hi there" in content
    assert "unreached" not in content  # the post-:quit line was never processed


def test_repl_blank_lines_are_skipped_not_errored():
    # A bare Enter (blank/whitespace-only line) re-prompts instead of driving an
    # empty-content turn (which run() would reject with an error frame).
    rc, out, _ = drive_repl(_stub(), ["", "   ", "hello"], as_json=True)
    assert rc == 0
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert len(_turns(frames)) == 1  # only "hello" drove a turn
    assert not any(f["type"] == "error" for f in frames)


def test_repl_json_mode_streams_raw_frames_including_terminals():
    # --json composes with --repl: raw event_to_dict frames (one per line), same as
    # --message --json — including ready and per-turn complete/turn_end.
    rc, out, _ = drive_repl(_stub(), ["hi"], as_json=True)
    assert rc == 0
    types = [json.loads(ln)["type"] for ln in out.splitlines() if ln.strip()]
    assert types[0] == "ready"
    for expected in ("ack", "content", "complete", "turn_end"):
        assert expected in types


def test_repl_prompt_is_drawn_to_stderr_keeping_stdout_clean():
    # With a TTY (show_prompt on), the '> ' prompt is drawn to stderr so stdout stays
    # the clean reply channel — the same stdout/stderr split --message uses.
    from langstage_vscode.sidecar import _run_repl

    stdin = io.StringIO("hi there\n")
    out, err = io.StringIO(), io.StringIO()
    rc = _run_repl(
        _stub(), spec=None, as_json=False,
        stdin=stdin, stdout=out, stderr=err, show_prompt=True,
    )
    assert rc == 0
    assert "> " in err.getvalue()  # prompt on stderr
    assert "> " not in out.getvalue()  # never on stdout
    assert "You said: hi there" in out.getvalue()


def test_repl_nonrunnable_spec_exits_1_with_clean_stdout():
    # A start failure (non-runnable spec) emits an error before any turn runs, so no
    # turn_end is seen: exit 1, actionable message on stderr, stdout empty — mirroring
    # --message. (A per-turn error inside a live session, by contrast, exits 0.)
    from langstage_vscode.sidecar import _run_repl

    stdin = io.StringIO("hi\n")
    out, err = io.StringIO(), io.StringIO()
    rc = _run_repl(
        42, spec="./x.py:make_graph", as_json=False,
        stdin=stdin, stdout=out, stderr=err, show_prompt=False,
    )
    assert rc == 1
    assert out.getvalue() == ""  # clean reply channel
    assert "not a runnable graph" in err.getvalue()


def test_repl_bad_turn_in_live_session_still_exits_clean():
    # An agent that raises mid-turn surfaces an error frame but the turn still reaches
    # turn_end, so a session that then ends on EOF exits 0 — the REPL is interactive,
    # not a one-shot whose exit code gates CI.
    rc, out, err = drive_repl(_boom_graph(), ["go"])
    assert rc == 0  # clean EOF termination despite the bad turn
    assert "kaboom" in err  # the failure was surfaced to stderr


def test_main_repl_demo_end_to_end(monkeypatch, tmp_path, capsys):
    # main() wires --repl: --demo answers real turns over a scripted stdin and exits 0
    # on :quit. (sys.stdin is a StringIO here, so isatty() is False -> no prompt noise.)
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi demo\n:quit\n"))
    assert main(["--demo", "--repl"]) == 0
    out = capsys.readouterr().out
    assert "You said: hi demo" in out
    assert '"type"' not in out  # human mode, clean channel


def test_main_repl_conflicts_with_message(monkeypatch, tmp_path, capsys):
    # --repl (multi-turn) and --message (one-shot) drive turns over different input
    # models; asking for both is a contradiction and errors before any turn. gh #84: this
    # mutex is a front-door fail() site, so in text mode it routes to STDERR (`error: ...`),
    # not a JSON frame on stdout (the mutex-in-detail assertions live in the gh #84 block).
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--repl", "--message", "hi"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "mutually exclusive" in captured.err


# ── gh #58: interrupt-aware turn drivers ─────────────────────────────
#
# Both --message and --repl used to render a turn that ends on a HITL interrupt(...)
# as a silent BLANK exit-0 — indistinguishable from an empty reply and the one
# sidecar capability with no CLI verification path. They now SURFACE the interrupt:
# a concise notice on stderr in human mode (stdout stays the clean reply channel),
# the raw `interrupt` frame on the --json stream, and — for one-shot --message — a
# distinct non-zero exit code (2) so an interrupt turn is scriptable. (Answering the
# interrupt inline in --repl is out of scope here — the issue's separate part 2.)


def test_interrupt_actions_handles_nested_and_unwrapped_shapes():
    # The core normalizer emits action_requests in two shapes: NESTED
    # ({"action_request": {"action": ...}}) for a single-dict interrupt({...}), and
    # UNWRAPPED ({"action": ...}) for the standard HumanInterrupt list interrupt([{...}]).
    # The extractor must name the action for either, and tolerate junk without raising.
    from langstage_vscode.sidecar import _interrupt_actions

    nested = {"action_requests": [{"action_request": {"action": "confirm", "args": {}},
                                   "description": "approve?"}]}
    assert _interrupt_actions(nested) == ["confirm"]
    unwrapped = {"action_requests": [{"action": "approve_tool", "args": {"x": 1}}]}
    assert _interrupt_actions(unwrapped) == ["approve_tool"]
    mixed = {"action_requests": [{"action": "a"}, {"action_request": {"action": "b"}}, "junk", {}]}
    assert _interrupt_actions(mixed) == ["a", "b"]
    assert _interrupt_actions({}) == []  # no action_requests key


def test_format_interrupt_notice_is_ascii_and_names_action_and_decisions():
    # The human-mode notice is CLI chrome, so it must be ASCII-safe (no U+23F8 pause
    # glyph) to survive a cp1252 console unmangled — the same rule the ASCII --help and
    # the _write_safe guard enforce. It names the pending action and the allowed decisions.
    from langstage_vscode.sidecar import _format_interrupt_notice

    frame = {"type": "interrupt",
             "action_requests": [{"action_request": {"action": "confirm", "args": {}},
                                  "description": "approve?"}],
             "allowed_decisions": ["reject", "edit", "respond", "approve"]}
    notice = _format_interrupt_notice(frame)
    assert notice.isascii(), notice
    assert "interrupt" in notice
    assert "confirm" in notice
    assert "reject | edit | respond | approve" in notice
    assert notice.endswith("\n")


def test_oneshot_sink_human_surfaces_interrupt_on_stderr_not_stdout():
    # gh #58: an interrupt turn must not be blank. Human mode keeps stdout the clean
    # reply channel and surfaces the pause on stderr, flagging saw_interrupt (which
    # drives --message's non-zero exit) without flagging saw_error.
    from langstage_vscode.sidecar import _OneShotSink

    out, err = io.StringIO(), io.StringIO()
    sink = _OneShotSink(out, err, as_json=False)

    sink.write(json.dumps({"type": "interrupt",
                           "action_requests": [{"action_request": {"action": "confirm", "args": {}}}],
                           "allowed_decisions": ["approve", "reject"]}) + "\n")
    sink.write(json.dumps({"type": "complete"}) + "\n")
    sink.write(json.dumps({"type": "turn_end", "session_id": "once"}) + "\n")
    sink.close_reply()

    assert out.getvalue() == ""  # stdout stays clean — no blank reply, no notice
    assert "interrupt: agent paused" in err.getvalue()
    assert "confirm" in err.getvalue()
    assert sink.saw_interrupt is True
    assert sink.saw_error is False


def test_oneshot_sink_json_forwards_interrupt_frame_verbatim():
    # gh #58: in --json mode the raw `interrupt` frame rides the stdout NDJSON stream
    # unchanged (a consumer keys on type == "interrupt") and no stderr notice is drawn —
    # one channel. saw_interrupt is still flagged for the exit code.
    from langstage_vscode.sidecar import _OneShotSink

    out, err = io.StringIO(), io.StringIO()
    sink = _OneShotSink(out, err, as_json=True)
    frame = {"type": "interrupt",
             "action_requests": [{"action_request": {"action": "confirm", "args": {}}}],
             "allowed_decisions": ["approve"]}
    sink.write(json.dumps(frame) + "\n")

    assert json.loads(out.getvalue().strip()) == frame  # verbatim on stdout
    assert sink.saw_interrupt is True
    assert err.getvalue() == ""  # --json keeps everything on the one channel


def test_oneshot_sink_normal_turn_sets_no_interrupt_flag():
    # Regression: a normal content turn must not trip saw_interrupt (so a clean
    # --message still exits 0, not 2).
    from langstage_vscode.sidecar import _OneShotSink

    out, err = io.StringIO(), io.StringIO()
    sink = _OneShotSink(out, err, as_json=False)
    sink.write(json.dumps({"type": "content", "content": "hi"}) + "\n")
    sink.write(json.dumps({"type": "complete"}) + "\n")
    sink.write(json.dumps({"type": "turn_end", "session_id": "once"}) + "\n")
    sink.close_reply()

    assert out.getvalue() == "hi\n"
    assert sink.saw_interrupt is False
    assert err.getvalue() == ""


def _run_once_interrupt(message, *, as_json):
    """Drive one-shot --message against the interrupt graph via the real _run_once
    driver, so its exit code and stdout/stderr split are exercised. _run_once takes the
    graph directly and bypasses config resolution, so no config isolation is needed."""
    from langstage_vscode.sidecar import _run_once

    return _run_once(_interrupt_graph(), message, spec=None, as_json=as_json)


def test_message_interrupt_turn_exits_2_with_notice_on_stderr(capsys):
    # gh #58 end-to-end (human mode): --message on an interrupt turn exits 2 (a distinct,
    # scriptable signal — not 0-and-blank, not 1-error), keeps stdout empty, and prints
    # the interrupt notice to stderr naming the action and the allowed decisions.
    rc = _run_once_interrupt("do it", as_json=False)
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # no silent blank on stdout, and no protocol noise
    assert "interrupt: agent paused" in captured.err
    assert "confirm" in captured.err
    assert "approve" in captured.err


def test_message_interrupt_turn_json_emits_frame_and_exits_2(capsys):
    # gh #58 end-to-end (--json): the raw `interrupt` frame is on stdout for a consumer
    # to key on, and the exit code is the same distinct 2 as human mode.
    rc = _run_once_interrupt("do it", as_json=True)
    assert rc == 2
    frames = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    interrupts = [f for f in frames if f.get("type") == "interrupt"]
    assert interrupts, [f.get("type") for f in frames]
    assert interrupts[0]["action_requests"][0]["action_request"]["action"] == "confirm"
    assert "approve" in interrupts[0]["allowed_decisions"]


def test_message_normal_turn_still_exits_0(monkeypatch, tmp_path, capsys):
    # Regression: a normal (non-interrupt) turn is unchanged — reply on stdout, exit 0.
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--message", "hello there"]) == 0
    out = capsys.readouterr().out
    assert "You said: hello there" in out


def test_repl_surfaces_interrupt_on_stderr_and_keeps_session_alive():
    # gh #58: --repl surfaces an interrupt turn on stderr (human mode) instead of a
    # blank, and — like a per-turn error in a live session — it does NOT end the
    # interactive session. stdout stays clean.
    # gh #63 refines the EXIT code: the session here ends (EOF) with the interrupt
    # still pending, i.e. never answered, so it now exits 2 — the same "paused awaiting
    # a decision" signal --message has carried since #58. Under #58 --repl always
    # returned 0 here because it had no way to answer; now that it does, walking away
    # from a pending interrupt is a real outcome worth a distinct code.
    rc, out, err = drive_repl(_interrupt_graph(), ["do it"])
    assert rc == 2  # interrupt left unanswered at EOF
    assert out == ""  # no blank reply on stdout
    assert "interrupt: agent paused" in err
    assert "confirm" in err


def test_repl_json_streams_raw_interrupt_frame():
    # gh #58: --json composes with --repl — the raw `interrupt` frame is on the NDJSON
    # stream for a scripting consumer, no stderr notice. (Exit 2 per gh #63: the
    # interrupt was never answered.)
    rc, out, err = drive_repl(_interrupt_graph(), ["do it"], as_json=True)
    assert rc == 2
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    interrupts = [f for f in frames if f.get("type") == "interrupt"]
    assert interrupts, [f.get("type") for f in frames]
    assert interrupts[0]["action_requests"][0]["action_request"]["action"] == "confirm"
    assert "interrupt: agent paused" not in err  # --json: raw frame only, no human notice


# ── gh #63: answering a HITL interrupt inline from --repl ────────────
#
# gh #58 made an interrupt VISIBLE; completing the interrupt -> decision round-trip
# still meant hand-writing NDJSON over the raw protocol, because a --repl line after
# an interrupt started a FRESH message turn (which just re-interrupts, so the answer
# silently looked accepted). Now a turn that ends on an interrupt puts the session in
# DECISION MODE: the next line becomes a `decision` command on the SAME session.
#
# The design contract these tests pin down:
#   * while pending, EVERY line (except :quit) is an answer attempt — a line that
#     isn't a valid decision is REFUSED on stderr and re-prompted, never downgraded
#     to a message and never swallowed;
#   * which verbs are valid comes from the interrupt frame's own allowed_decisions,
#     never a hard-coded list;
#   * `:decision <verb>` is the explicit form (same `:` namespace as `:quit`), and a
#     bare verb works only while pending, where it is unambiguous;
#   * exit 0 when the interrupt was answered, 2 when the session ends with one still
#     pending.


def _interrupt_graph_limited():
    """The issue's own minimal HITL agent (gh #63): a HumanInterrupt LIST whose config
    allows only ignore/accept, so the frame advertises exactly `reject | approve` — the
    fixture that proves the verb list is read off the FRAME, not hard-coded."""
    def ask(state):
        req = {"action_request": {"action": "delete_file", "args": {"path": "x"}},
               "config": {"allow_ignore": True, "allow_accept": True},
               "description": "Delete file x?"}
        decision = interrupt([req])
        return {"messages": [AIMessage(content=f"got {decision}")]}

    b = StateGraph(MessagesState)
    b.add_node("ask", ask)
    b.add_edge(START, "ask")
    b.add_edge("ask", END)
    return b.compile(checkpointer=MemorySaver())


def _acks(frames):
    """The `ack` refs in order — `message` vs `decision` is exactly what the issue's
    --json trace uses to show whether the round-trip completed."""
    return [f.get("ref") for f in frames if f.get("type") == "ack"]


def test_parse_repl_decision_maps_verbs_and_payloads():
    # The four canonical langchain HITL shapes, built from an answer line: bare verb,
    # optional/required text (-> `message`), and a JSON object merged in (-> the full
    # typed shape, e.g. EditDecision's `edited_action`). Verb matching is
    # case-insensitive; the frame's spelling wins the `type` key.
    from langstage_vscode.sidecar import _parse_repl_decision

    allowed = ["reject", "edit", "respond", "approve"]
    assert _parse_repl_decision("approve", allowed) == ({"type": "approve"}, None)
    assert _parse_repl_decision("APPROVE", allowed) == ({"type": "approve"}, None)
    assert _parse_repl_decision("reject", allowed) == ({"type": "reject"}, None)
    assert _parse_repl_decision("reject too risky", allowed) == (
        {"type": "reject", "message": "too risky"}, None)
    assert _parse_repl_decision("respond do not delete it", allowed) == (
        {"type": "respond", "message": "do not delete it"}, None)
    decision, err = _parse_repl_decision('edit {"edited_action": {"name": "x"}}', allowed)
    assert err is None
    assert decision == {"edited_action": {"name": "x"}, "type": "edit"}


def test_parse_repl_decision_refuses_unadvertised_verbs_and_bad_payloads():
    # Constraint: nothing is guessed. A verb the frame didn't advertise is refused even
    # when it is a canonical HITL verb; a missing required payload, a malformed JSON
    # payload, and junk trailing a no-payload verb are all refused with a reason —
    # never coerced into a decision the agent didn't offer.
    from langstage_vscode.sidecar import _parse_repl_decision

    limited = ["reject", "approve"]
    decision, err = _parse_repl_decision("edit {}", limited)
    assert decision is None and "not a decision this interrupt allows" in err
    decision, err = _parse_repl_decision("looks fine to me", limited)
    assert decision is None and "`looks`" in err

    full = ["reject", "edit", "respond", "approve"]
    decision, err = _parse_repl_decision("respond", full)
    assert decision is None and "needs a payload" in err
    decision, err = _parse_repl_decision("edit", full)
    assert decision is None and "needs a payload" in err
    decision, err = _parse_repl_decision("edit oops", full)
    assert decision is None and "JSON" in err
    decision, err = _parse_repl_decision('edit {"broken"', full)
    assert decision is None and "not valid JSON" in err
    decision, err = _parse_repl_decision("approve sure thing", full)
    assert decision is None and "takes no payload" in err


def test_repl_interrupt_notice_tells_you_what_to_type_from_the_frame():
    # Discoverability: in --repl the notice prints the literal syntax to type instead of
    # pointing at the raw protocol, and the verbs it offers come from THIS frame's
    # allowed_decisions — an interrupt that allows only reject/approve must not
    # advertise edit/respond. Still ASCII-only (cp1252 console rule).
    from langstage_vscode.sidecar import _format_interrupt_notice

    frame = {"type": "interrupt",
             "action_requests": [{"action": "delete_file", "args": {"path": "x"}}],
             "allowed_decisions": ["reject", "approve"]}
    notice = _format_interrupt_notice(frame, repl=True)
    assert notice.isascii(), notice
    assert ":decision <verb>" in notice
    assert "reject | approve" in notice
    assert "edit" not in notice and "respond" not in notice

    # The one-shot flavour is unchanged: --message exits at turn_end, so it can only
    # point at the raw `decision` command.
    oneshot = _format_interrupt_notice(frame)
    assert "resume by sending a `decision` command" in oneshot
    assert ":decision" not in oneshot


def test_repl_sink_tracks_pending_interrupt_and_clears_it_on_the_next_ack():
    # The state the REPL loop branches on. An `interrupt` frame sets it; the next `ack`
    # (a command was accepted, i.e. it is being acted on) clears it — so a resumed turn
    # that completes leaves nothing pending, and one that interrupts AGAIN stays pending.
    from langstage_vscode.sidecar import _ReplSink

    sink = _ReplSink(io.StringIO(), io.StringIO(), as_json=True)
    assert sink.pending_interrupt is None
    sink.write(json.dumps({"type": "ack", "ref": "message"}) + "\n")
    frame = {"type": "interrupt", "action_requests": [], "allowed_decisions": ["approve"]}
    sink.write(json.dumps(frame) + "\n")
    sink.write(json.dumps({"type": "turn_end", "session_id": "repl"}) + "\n")
    assert sink.pending_interrupt == frame  # survives turn_end: that IS the pending state
    sink.write(json.dumps({"type": "ack", "ref": "decision"}) + "\n")
    assert sink.pending_interrupt is None
    sink.write(json.dumps({"type": "content", "content": "resumed"}) + "\n")
    sink.write(json.dumps({"type": "turn_end", "session_id": "repl"}) + "\n")
    assert sink.pending_interrupt is None


def test_repl_bare_verb_answers_the_pending_interrupt_and_resumes_the_turn():
    # THE round-trip (the issue's headline): `do it` interrupts, `approve` answers it,
    # and the resumed turn's reply is printed — one command, no hand-written NDJSON.
    # The reply echoes the decision payload, proving it reached the agent intact.
    rc, out, err = drive_repl(_interrupt_graph(), ["do it", "approve"])
    assert rc == 0  # the interrupt was answered, so the session ends clean
    assert out == "resumed with: {'decisions': [{'type': 'approve'}]}\n"
    assert "interrupt: agent paused" in err  # the pause was still surfaced
    assert "not a decision" not in err


def test_repl_json_trace_shows_ack_decision_not_a_second_ack_message():
    # The machine-readable proof from the issue. Before the fix the answer line emitted
    # `ack message` + a SECOND `interrupt` (a fresh turn that just re-interrupted);
    # after it the trace is ack message -> interrupt -> turn_end -> ack DECISION ->
    # content -> turn_end, exactly like the hand-written raw-protocol transcript.
    rc, out, _ = drive_repl(_interrupt_graph(), ["do it", "approve"], as_json=True)
    assert rc == 0
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert _acks(frames) == ["message", "decision"]
    assert [f["type"] for f in frames if f["type"] == "interrupt"] == ["interrupt"]
    turns = _turns(frames)
    assert len(turns) == 2
    assert _reply(turns[1]) == "resumed with: {'decisions': [{'type': 'approve'}]}"


def test_repl_explicit_decision_command_answers_the_interrupt():
    # `:decision <verb>` — the explicit form, in the same `:`-prefixed namespace as
    # `:quit`, and the form the notice tells you to type. Payload verbs compose with it.
    rc, out, _ = drive_repl(
        _interrupt_graph(), ["do it", ":decision respond leave it alone"], as_json=True)
    assert rc == 0
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert _acks(frames) == ["message", "decision"]
    assert _reply(_turns(frames)[1]) == (
        "resumed with: {'decisions': [{'type': 'respond', 'message': 'leave it alone'}]}")


def test_repl_invalid_line_while_pending_is_refused_not_sent_as_a_message():
    # Constraint 1, the whole point: a line that isn't a valid decision while an
    # interrupt is pending must be OBVIOUS and RECOVERABLE. It is not silently sent as
    # a new message (the gh #63 bug — that just re-interrupts and looks accepted) and
    # not swallowed: it is named on stderr with the verbs this frame allows, the
    # interrupt stays pending, and the very next line still answers it.
    rc, out, err = drive_repl(
        _interrupt_graph(), ["do it", "looks fine to me", "approve"], as_json=True)
    assert rc == 0  # the retry landed
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert _acks(frames) == ["message", "decision"]  # the junk line drove NO turn
    assert len(_turns(frames)) == 2
    assert "not a decision: 'looks fine to me'" in err
    assert "still pending" in err
    assert "reject" in err and "approve" in err  # the frame's verbs, repeated
    assert _reply(_turns(frames)[1]) == "resumed with: {'decisions': [{'type': 'approve'}]}"


def test_repl_abandoning_a_pending_interrupt_exits_2():
    # `:quit` is checked before decision parsing, so it is always the way out — but the
    # interrupt was never answered, so the session reports the gh #58 "paused awaiting a
    # decision" code rather than a false-clean 0.
    rc, out, err = drive_repl(_interrupt_graph(), ["do it", "nonsense", ":quit"])
    assert rc == 2
    assert out == ""
    assert "not a decision" in err


def test_repl_decision_verbs_are_read_off_the_frame_not_a_hardcoded_list():
    # The issue's own agent advertises only `reject | approve` (allow_ignore +
    # allow_accept). `edit` is a canonical HITL verb but THIS interrupt doesn't allow
    # it, so it is refused; `approve` resumes.
    rc, out, err = drive_repl(
        _interrupt_graph_limited(), ["do it", "edit {}", "approve"], as_json=True)
    assert rc == 0
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert _acks(frames) == ["message", "decision"]
    assert "not a decision this interrupt allows" in err
    assert _reply(_turns(frames)[1]) == "got {'decisions': [{'type': 'approve'}]}"


def test_repl_decision_lands_on_the_live_session_thread():
    # Session/thread continuity: both turns carry the REPL's one session_id, so the
    # decision resumes THAT thread's pending interrupt (a decision on a fresh thread
    # would have nothing to resume and could not produce the resumed reply).
    rc, out, _ = drive_repl(_interrupt_graph(), ["do it", "approve"], as_json=True)
    assert rc == 0
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    ends = [f for f in frames if f["type"] == "turn_end"]
    assert [f["session_id"] for f in ends] == ["repl", "repl"]


def test_repl_decision_command_outside_decision_mode_is_refused():
    # `:decision ...` with nothing paused answers nothing — it must not be chatted at
    # the agent as text, and must not fabricate a `decision` command the sidecar would
    # reject. It is named on stderr and the session continues normally.
    rc, out, err = drive_repl(_stub(), [":decision approve", "hi there"], as_json=True)
    assert rc == 0
    frames = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert _acks(frames) == ["message"]  # only "hi there" drove a turn
    assert not any(f["type"] == "error" for f in frames)
    assert "no interrupt is pending" in err
    content = "".join(f.get("content", "") for f in frames if f["type"] == "content")
    assert ":decision" not in content  # never sent as chat text


def test_repl_bare_verb_with_nothing_pending_is_still_an_ordinary_message():
    # Regression + the reason a bare verb is only accepted while pending: outside
    # decision mode "approve" is ordinary chat text, and stays so.
    rc, out, err = drive_repl(_stub(), ["approve"])
    assert rc == 0
    assert "You said: approve" in out
    assert err == ""


# ── gh #65: a valid `decision` with NO pending interrupt must error ──────────
#
# gh #33 closed the empty-list half (`decisions: []`) — a decision that is invalid by
# SHAPE. gh #65 is the non-empty sibling: a well-formed `decisions: [{"type":"approve"}]`
# arriving on a thread that was never interrupted still has no interrupt to resume, so
# it must emit the same `error` frame (the invariant the #33 comment already states) —
# not ack + drive a spurious turn that ends in a false `complete`. The raw run() path
# now tracks pending-interrupt state per session_id/thread off its own emitted frames.


def test_decision_with_no_pending_interrupt_errors_not_spurious_turn():
    # gh #65: the headline. A valid decision on a fresh, never-interrupted session is
    # answered with an `error` frame and drives NO turn — no ack, no content, no
    # (false) `complete`, no turn_end. Contrast the #33 empty-list case, which errors
    # on shape; this one is well-shaped but has nothing to resume.
    events = drive(_stub(), [
        {"type": "decision", "session_id": "fresh", "decisions": [{"type": "approve"}]},
        {"type": "shutdown"},
    ])
    assert any(e["type"] == "error" and "no interrupt pending for session 'fresh'" in e["error"]
               for e in events), [e for e in events if e["type"] == "error"]
    assert not any(e.get("type") == "ack" and e.get("ref") == "decision" for e in events)
    assert not any(e["type"] == "turn_end" for e in events)
    assert not any(e["type"] == "complete" for e in events)
    assert not any(e["type"] == "content" for e in events)


def test_decision_pending_state_is_keyed_per_session():
    # gh #65: the fix must be keyed to the RIGHT thread — the sidecar can hold several
    # sessions. A message that interrupts session A leaves ONLY A pending: the same
    # decision resumes A but still errors on a never-interrupted session B.
    events = drive(_interrupt_graph(), [
        {"type": "message", "session_id": "A", "content": "go"},                       # A interrupts
        {"type": "decision", "session_id": "B", "decisions": [{"type": "approve"}]},    # B never paused
        {"type": "decision", "session_id": "A", "decisions": [{"type": "approve"}]},    # A resumes
        {"type": "shutdown"},
    ])
    assert any(e["type"] == "error" and "no interrupt pending for session 'B'" in e["error"]
               for e in events)
    # A's decision is acted on and the turn resumes with the decision payload intact.
    assert any(e.get("type") == "ack" and e.get("ref") == "decision" for e in events)
    text = "".join(e.get("content", "") for e in events if e["type"] == "content")
    assert "resumed with" in text and "approve" in text


def test_decision_rejected_after_the_interrupt_was_already_answered():
    # gh #65: pending state clears once the interrupt is resumed and the turn completes,
    # so a SECOND decision (a double-send, or one after the interrupt already cleared)
    # has nothing to resume and must error — exactly the "double-send" case the issue
    # names — rather than drive another spurious turn.
    events = drive(_interrupt_graph(), [
        {"type": "message", "session_id": "s", "content": "go"},                       # interrupts
        {"type": "decision", "session_id": "s", "decisions": [{"type": "approve"}]},    # resumes -> completes
        {"type": "decision", "session_id": "s", "decisions": [{"type": "approve"}]},    # nothing pending now
        {"type": "shutdown"},
    ])
    acks = [e for e in events if e.get("type") == "ack" and e.get("ref") == "decision"]
    assert len(acks) == 1  # only the first decision was acted on
    assert any(e["type"] == "error" and "no interrupt pending" in e["error"] for e in events)


# ── gh #67: cooperative per-turn `cancel` ────────────────────────────────────
#
# A `cancel` stops the in-flight turn WITHOUT tearing down the process, so the
# long-lived session + in-process checkpointer survive (the mid-stream cancel + memory
# survival is exercised end-to-end in test_agui_sidecar.py, which has the streaming
# fixtures). Here: the command is known (not "unknown command type"), and a cancel with
# nothing in flight is refused cleanly rather than crashing or acking.


def test_cancel_with_no_turn_in_flight_errors_gracefully():
    # gh #67: `cancel` is now a known command. With no turn streaming for the session it
    # gets a clean `error` (like the decision/message guards) — not the old
    # "unknown command type: 'cancel'", and not a crash or an ack.
    events = drive(_stub(), [
        {"type": "cancel", "session_id": "s"},
        {"type": "shutdown"},
    ])
    assert any(e["type"] == "error" and "no turn in progress for session 's'" in e["error"]
               for e in events), [e for e in events if e["type"] == "error"]
    assert not any("unknown command" in e.get("error", "") for e in events)
    assert not any(e["type"] in ("cancelled", "ack", "turn_end") for e in events)


def test_run_turn_cooperative_cancel_stops_stream_before_complete():
    # gh #67: the streaming primitive. With a cancel requested between frames,
    # _run_turn_agui stops the turn early — emitting NO `complete` — and reports
    # cancelled. An injected should_cancel makes this deterministic without threads:
    # it lets the first frame through, then fires.
    from langstage_vscode.agui_stream import build_session_agent
    from langstage_vscode.sidecar import _run_turn_agui

    agent = build_session_agent(_stub())
    emitted: list = []
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # frame 1 passes; cancel before frame 2

    saw_interrupt, cancelled = _run_turn_agui(
        agent, "hi there", None, {"configurable": {"thread_id": "s"}}, 50_000,
        emitted.append, should_cancel=should_cancel,
    )
    assert cancelled is True
    assert saw_interrupt is False
    types = [f.get("type") for f in emitted]
    assert "complete" not in types  # cancelled before the turn's own `complete`
    assert calls["n"] >= 2


# ── gh #66: the interrupt->decision trace emits `complete` on BOTH turns ──────
#
# The runtime emits a `complete` after the `interrupt` turn AND after the resumed
# `content` turn. The README/CHANGELOG trace had regressed to dropping both. This
# pins the real sequence so the docs (guarded in test_packaging.py) can be checked
# against it, and so the behavior itself can't silently change.


def test_interrupt_decision_trace_emits_complete_on_both_turns():
    # gh #66: the full round-trip frame sequence. An interrupt turn is `interrupt` ->
    # `complete` -> `turn_end` (paused, but still `complete`); the resumed turn is
    # `content` -> `complete` -> `turn_end`. Both `complete` frames are present.
    events = drive(_interrupt_graph(), [
        {"type": "message", "session_id": "s", "content": "do it"},
        {"type": "decision", "session_id": "s", "decisions": [{"type": "approve"}]},
        {"type": "shutdown"},
    ])
    types = [e["type"] for e in events]
    # Collapse any run of consecutive `content` frames so the assertion is about the
    # protocol frames, not how finely the reply is chunked.
    collapsed = [t for i, t in enumerate(types)
                 if t != "content" or (i == 0 or types[i - 1] != "content")]
    assert collapsed == [
        "ready",
        "ack", "interrupt", "complete", "turn_end",
        "ack", "content", "complete", "turn_end",
    ], types


# ── gh #77: --demo=tools wires the demo extractors so `extraction` is emitted ──
#
# Three doc surfaces (README/--help/CHANGELOG) promise --demo=tools exercises an
# `extraction` frame, but the sidecar drove iter_event_frames with the default
# extractors=(), so `extraction` was UNREACHABLE — only tool_start/tool_end/reasoning/
# interrupt ever appeared. The fix wires the demo's own paired extractors
# (demo_extractors(), the same source the demo graph uses) on the --demo=tools path.


def test_main_demo_tools_emits_extraction_frame(tmp_path):
    # gh #77: the verbatim README/--help command must now emit the advertised
    # `extraction` frame, alongside the tool_start/tool_end it already produced. Driven
    # as a SUBPROCESS (fresh interpreter -> clean demo-graph checkpointer) so it runs the
    # exact CLI the docs hand a new adopter, immune to the shared in-process "once" thread
    # the other --demo=tools --message tests leave state on. Mirrors the issue's repro.
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)  # no stray real config
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode",
         "--demo=tools", "--message", "please use a tool", "--json"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    types = {json.loads(ln).get("type") for ln in proc.stdout.splitlines() if ln.strip()}
    assert "extraction" in types, proc.stdout  # the frame that was unreachable before the fix
    assert "tool_start" in types and "tool_end" in types  # not regressed


def test_run_extractors_gate_the_extraction_frame():
    # gh #77: the mechanism. The SAME demo tools graph emits `extraction` ONLY when the
    # extractors are wired into run(); with the default extractors=() it does not — so
    # the wiring, not the graph, is what was missing (mirrors the issue's core-API proof).
    from langstage_core.demo.tools import demo_extractors

    graph = load_agent_spec("langstage_core.demo.tools:graph")
    cmds = [
        {"type": "message", "session_id": "s", "content": "please use a tool"},
        {"type": "shutdown"},
    ]

    def types_for(extractors):
        stdin = io.StringIO("".join(json.dumps(c) + "\n" for c in cmds))
        stdout = io.StringIO()
        run(graph, stdin, stdout, extractors=extractors)
        return {json.loads(ln)["type"] for ln in stdout.getvalue().splitlines() if ln.strip()}

    assert "extraction" not in types_for(())            # what the sidecar used to do
    assert "extraction" in types_for(demo_extractors())  # the one-line fix


# ── gh #78: text-mode load failure -> `error: <msg>` on stderr, clean stdout ──
#
# --message/--repl are the human/CLI front doors: text mode keeps stdout the clean
# reply channel and puts diagnostics on stderr, and --json is the switch for raw
# frames. A load failure used to ignore both — it printed the raw JSON error frame to
# STDOUT in every mode (via the raw-protocol fail() helper), poisoning a captured
# `reply=$(... --message ...)` with a JSON blob the moment you typo an agent path.


def test_main_message_load_failure_text_mode_errors_on_stderr(monkeypatch, tmp_path, capsys):
    # gh #78: text mode -> `error: <msg>` on stderr (same shape as the runtime-error
    # path), stdout stays empty — no raw JSON frame leak.
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_AGENT_SPEC", "no_such_module:graph")
    assert main(["--message", "hello"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # clean reply channel — no JSON error blob on stdout
    assert captured.err.startswith("error: ")
    assert "failed to load agent" in captured.err
    assert '"type"' not in captured.out  # not the machine JSON frame


def test_main_message_load_failure_json_mode_keeps_frame_on_stdout(monkeypatch, tmp_path, capsys):
    # gh #78: with --json a load failure still emits the JSON error frame on stdout —
    # that is exactly what --json is for; only TEXT mode routes to stderr.
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_AGENT_SPEC", "no_such_module:graph")
    assert main(["--message", "hello", "--json"]) == 1
    captured = capsys.readouterr()
    frame = json.loads(captured.out.strip())
    assert frame["type"] == "error" and "failed to load agent" in frame["error"]
    assert captured.err == ""  # --json keeps everything on the one channel


def test_main_repl_load_failure_text_mode_errors_on_stderr(monkeypatch, tmp_path, capsys):
    # gh #78: --repl shares the same load step and the same text-mode contract.
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_AGENT_SPEC", "no_such_module:graph")
    monkeypatch.setattr("sys.stdin", io.StringIO("hi\n:quit\n"))
    assert main(["--repl"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "failed to load agent" in captured.err


def test_main_raw_protocol_load_failure_keeps_json_frame_on_stdout(monkeypatch, tmp_path, capsys):
    # gh #78 regression guard: the raw stdio loop the VS Code extension drives is
    # UNCHANGED — a load failure is the `{"type":"error"}` NDJSON frame on stdout (what
    # that consumer wants), not routed to stderr.
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_AGENT_SPEC", "no_such_module:graph")
    assert main([]) == 1
    frame = json.loads(capsys.readouterr().out.strip())
    assert frame["type"] == "error" and "failed to load agent" in frame["error"]


# ── gh #79: --selfcheck drives its preflight turn from the workspace cwd ──
#
# Every runtime path (run()/--message/--repl) os.chdir(workspace_root()) after
# resolving the spec (ADR 0006), so a BYO agent's raw relative file I/O lands in the
# workspace. --selfcheck skipped that chdir and drove its turn from the LAUNCH cwd, so
# a cwd-sensitive agent was preflighted in the wrong directory -> a green selfcheck
# gave false confidence about where the runtime's file behavior actually lands (#28).


def test_selfcheck_drives_turn_from_workspace_cwd(monkeypatch, tmp_path, capsys):
    # gh #79: the issue's cwd_probe. An agent whose turn does a raw RELATIVE write must,
    # during selfcheck, write into the WORKSPACE (like the real run path) — not the
    # launch cwd. Spec resolved AFTER an absolute path so the chdir can't change where it
    # loads from.
    import os
    from pathlib import Path

    _isolate_config(monkeypatch, tmp_path)  # chdir's to tmp_path (the launch cwd)
    agent = tmp_path / "cwd_probe.py"
    agent.write_text(
        "import os\n"
        "from langgraph.graph import StateGraph, START, END, MessagesState\n"
        "from langchain_core.messages import AIMessage\n"
        "def r(state):\n"
        "    open('agent_wrote_here.txt', 'w').write(os.getcwd())\n"
        "    return {'messages': [AIMessage(content=os.getcwd())]}\n"
        "b = StateGraph(MessagesState)\n"
        "b.add_node('r', r)\n"
        "b.add_edge(START, 'r'); b.add_edge('r', END)\n"
        "graph = b.compile()\n"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    rc = main(["--selfcheck", "--agent", f"{agent}:graph", "--workspace", str(ws)])
    assert rc == 0, capsys.readouterr().err
    # The relative write landed in the WORKSPACE, not the launch cwd.
    assert (ws / "agent_wrote_here.txt").exists(), "selfcheck turn did not run in the workspace"
    assert not (tmp_path / "agent_wrote_here.txt").exists(), "selfcheck polluted the launch cwd"
    # ...and the cwd the turn actually saw resolves to the workspace.
    assert Path((ws / "agent_wrote_here.txt").read_text()).resolve() == ws.resolve()


# ── gh #88: the agent module is IMPORTED from the workspace cwd ──
#
# gh #79 moved the preflight TURN into the workspace, but the chdir still landed AFTER
# load_agent_spec() imported the module — so an agent that does module-level (import-time)
# relative I/O (loading a prompt/config/data file) was imported from the LAUNCH cwd. In the
# real extension run path that's invisible (the extension spawns the sidecar with cwd ==
# workspace), but the documented preflight (--selfcheck/--message with --workspace from
# another dir) false-FAILed a working agent. The chdir now happens BEFORE the import in both
# the run path and --selfcheck; a relative -a spec is absolutized against the launch cwd
# first so it still loads from where the user pointed.


def _import_probe_agent(tmp_path):
    """Write an agent that at IMPORT time records os.getcwd() and reads a WORKSPACE-relative
    file (prompt.txt). The file exists only in the workspace, so an import from the launch
    cwd FileNotFoundError's — proving where the module was imported from."""
    agent = tmp_path / "import_probe.py"
    agent.write_text(
        "import os\n"
        "_IMPORT_CWD = os.getcwd()\n"
        "with open('prompt.txt') as f:\n"            # import-time relative read
        "    _SYS = f.read().strip()\n"
        "from langgraph.graph import StateGraph, START, END, MessagesState\n"
        "from langchain_core.messages import AIMessage\n"
        "def r(state):\n"
        "    return {'messages': [AIMessage(content=_IMPORT_CWD + '|' + _SYS)]}\n"
        "b = StateGraph(MessagesState)\n"
        "b.add_node('r', r)\n"
        "b.add_edge(START, 'r'); b.add_edge('r', END)\n"
        "graph = b.compile()\n"
    )
    return agent


def test_message_imports_agent_from_workspace_cwd(monkeypatch, tmp_path, capsys):
    # gh #88: the run path chdirs into the workspace BEFORE importing the agent, so an
    # agent that reads a workspace-relative file AT IMPORT TIME loads it. Launched from a
    # DIFFERENT cwd (tmp_path) with --workspace pointing at ws; without the fix the import
    # runs at the launch cwd and FileNotFoundError's on prompt.txt.
    from pathlib import Path

    _isolate_config(monkeypatch, tmp_path)  # chdir's to tmp_path (the launch cwd)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "prompt.txt").write_text("hello-from-workspace")
    agent = _import_probe_agent(tmp_path)  # agent lives OUTSIDE the workspace
    rc = main(["--agent", f"{agent}:graph", "--workspace", str(ws), "--message", "hi"])
    out = capsys.readouterr().out
    assert rc == 0, out  # did NOT false-FAIL: the import-time relative read succeeded
    import_cwd, _, sys_txt = out.strip().partition("|")
    assert Path(import_cwd).resolve() == ws.resolve()  # module was imported FROM the workspace
    assert sys_txt == "hello-from-workspace"           # ...and read the workspace-relative file


def test_selfcheck_imports_agent_from_workspace_cwd(monkeypatch, tmp_path, capsys):
    # gh #88: the documented preflight (--selfcheck --workspace, run from another dir) must
    # import the agent from the workspace too, so an agent that does import-time relative
    # I/O is not false-FAILed. prompt.txt exists only in ws; a launch-cwd import would raise
    # FileNotFoundError and FAIL the preflight (rc 1). With the fix the preflight is green.
    _isolate_config(monkeypatch, tmp_path)  # chdir's to tmp_path (the launch cwd)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "prompt.txt").write_text("hello-from-workspace")
    agent = _import_probe_agent(tmp_path)
    rc = main(["--selfcheck", "--agent", f"{agent}:graph", "--workspace", str(ws)])
    assert rc == 0, capsys.readouterr().err  # was a false FAIL: FileNotFoundError at import


def test_absolutize_spec_path_leaves_dotted_and_bare_specs_untouched():
    # gh #88 unit: only a FILE-PATH spec is absolutized (so a later chdir can't move it);
    # a dotted module spec and a suffix-less spec pass through unchanged (core handles them).
    import os

    from langstage_vscode.sidecar import _absolutize_spec_path

    assert _absolutize_spec_path("langstage_core.demo.stub:graph") == "langstage_core.demo.stub:graph"
    assert _absolutize_spec_path("no_colon_here") == "no_colon_here"
    # A relative file-path spec becomes absolute against the CURRENT cwd, obj name preserved.
    out = _absolutize_spec_path("./agent.py:graph")
    assert out == f"{os.path.abspath('./agent.py')}:graph"
    assert os.path.isabs(out.rpartition(":")[0])


# ── gh #80: a valid-JSON non-object line errors gracefully (no crash) ──
#
# A line that is valid JSON but not an object (a bare string/number/array/null/bool)
# parsed fine past the json.loads guard, then hit cmd.get("type") on a non-dict and
# raised an uncaught AttributeError that unwound out of run()/main() and killed the
# whole process — tearing down every session + in-process checkpointer. Every sibling
# malformed input (bad JSON, unknown type, empty content) emits an `error` and continues.


def test_non_object_json_line_errors_and_loop_survives():
    # gh #80: every non-object JSON scalar/array/null must emit an `error` frame and keep
    # the loop alive — a valid message AFTER the bad line is still processed to completion,
    # not lost to a process crash.
    for bad in ('"oops"', "42", "[1, 2, 3]", "null", "true"):
        stdin = io.StringIO(
            bad + "\n"
            + json.dumps({"type": "message", "session_id": "s", "content": "survives"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()
        run(_stub(), stdin, stdout)  # must NOT raise (was an uncaught AttributeError)
        events = [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]
        types = [e["type"] for e in events]
        assert any(e["type"] == "error" and "must be a JSON object" in e["error"]
                   for e in events), bad
        # loop survived: the message after the bad line ran to completion
        assert "complete" in types and "turn_end" in types, (bad, types)
        content = "".join(e.get("content", "") for e in events if e["type"] == "content")
        assert "survives" in content, bad


# ── gh #82: --show-config surfaces unknown/misspelled langstage.toml keys ──
#
# --show-config is the sidecar's "why isn't my agent loading?" verb, but a typo'd key
# in langstage.toml (specc, modell, a wrong [agents] section) was silently dropped —
# the single most common config mistake, invisible on the one verb built to catch it.
# core 1.0.32 collects those unknown keys (HostConfig.unknown_toml_keys()) and both
# describe() and config_dict() now surface them, so the sidecar's --show-config (which
# renders through both) reports them with no local wiring — just the >=1.0.32 floor.


def test_show_config_text_surfaces_unknown_toml_key(monkeypatch, tmp_path, capsys):
    # gh #82: a valid [agent].spec plus a typo'd sibling key `modell` — the human report
    # must name the ignored key so a one-character typo isn't invisible.
    _isolate_config(monkeypatch, tmp_path)
    (tmp_path / "langstage.toml").write_text(
        '[agent]\nspec = "my_agent.py:graph"\nmodell = "gpt-4o"\n'
    )
    assert main(["--show-config"]) == 0
    out = capsys.readouterr().out
    assert "unknown TOML keys" in out
    assert "agent.modell" in out


def test_show_config_json_surfaces_unknown_toml_key(monkeypatch, tmp_path, capsys):
    # gh #82: the machine-readable form reports the same under toml.unknown_keys, so the
    # extension / CI can flag a typo'd config without scraping the human text.
    _isolate_config(monkeypatch, tmp_path)
    (tmp_path / "langstage.toml").write_text(
        '[agent]\nspec = "my_agent.py:graph"\nmodell = "gpt-4o"\n'
    )
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["toml"]["unknown_keys"] == ["agent.modell"]


def test_show_config_clean_toml_has_no_unknown_keys(monkeypatch, tmp_path, capsys):
    # gh #82: a clean, fully-recognized toml reports NO unknown keys — the notice fires
    # only on a genuine typo/misplacement, not on every file.
    _isolate_config(monkeypatch, tmp_path)
    (tmp_path / "langstage.toml").write_text('[agent]\nspec = "my_agent.py:graph"\n')
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["toml"]["unknown_keys"] == []
    assert main(["--show-config"]) == 0  # and the human text omits the notice line
    assert "unknown TOML keys" not in capsys.readouterr().out


def test_show_config_surfaces_misspelled_section(monkeypatch, tmp_path, capsys):
    # gh #82: a whole misspelled section `[agents]` (not `[agent]`) is dropped just as
    # silently — its key must show up as an unknown dotted key too.
    _isolate_config(monkeypatch, tmp_path)
    (tmp_path / "langstage.toml").write_text('[agents]\nspec = "my_agent.py:graph"\n')
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["toml"]["unknown_keys"] == ["agents.spec"]


# ── gh #83: --traceback / LANGSTAGE_DEBUG shows WHERE the agent crashed ──
#
# Every agent-error path surfaced only `Type: message`, never the traceback, so on a
# real failing agent you couldn't tell which line threw. core 1.0.32 carries the failing
# turn's traceback in the terminal `error` frame when LANGSTAGE_DEBUG is set; --traceback
# (alias --debug) sets that env for the turn and the sinks render the frame's `traceback`
# — stderr in text mode (stdout stays the clean reply channel), the raw frame in --json.


def _deep_crash_agent(tmp_path):
    """Write an agent whose node crashes deep in a helper (a KeyError two frames down),
    so a rendered traceback must name the helper line, not just the exception type."""
    agent = tmp_path / "deep_crash.py"
    agent.write_text(
        "from langgraph.graph import StateGraph, MessagesState, START, END\n"
        "def helper(x):\n"
        "    return x['missing_key']\n"
        "def respond(state):\n"
        "    return helper({})\n"
        "g = StateGraph(MessagesState)\n"
        "g.add_node('respond', respond)\n"
        "g.add_edge(START, 'respond')\n"
        "g.add_edge('respond', END)\n"
        "graph = g.compile()\n"
    )
    return f"{agent}:graph"


def test_message_traceback_flag_shows_crash_location_on_stderr(monkeypatch, tmp_path, capsys):
    # gh #83: WITH --traceback, an erroring agent still prints `error: <type>: <msg>` and,
    # additionally, the full Python traceback to STDERR — naming the helper line that threw
    # — while stdout stays empty (the clean reply channel).
    _isolate_config(monkeypatch, tmp_path)
    spec = _deep_crash_agent(tmp_path)
    assert main(["--agent", spec, "--message", "hi", "--traceback"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout stays the clean reply channel
    assert "error: KeyError" in captured.err
    assert "Traceback (most recent call last)" in captured.err
    assert "in helper" in captured.err  # names WHERE it crashed, not just the type


def test_message_without_traceback_flag_shows_only_the_message(monkeypatch, tmp_path, capsys):
    # gh #83: the default (no flag, no env) is 100% unchanged — the terse `error:` line on
    # stderr and NO traceback, so normal output is byte-identical to before.
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.delenv("LANGSTAGE_DEBUG", raising=False)
    spec = _deep_crash_agent(tmp_path)
    assert main(["--agent", spec, "--message", "hi"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: KeyError" in captured.err
    assert "Traceback (most recent call last)" not in captured.err
    assert "in helper" not in captured.err


def test_message_debug_alias_matches_traceback(monkeypatch, tmp_path, capsys):
    # gh #83: --debug is an alias for --traceback (the muscle-memory spelling) and behaves
    # identically.
    _isolate_config(monkeypatch, tmp_path)
    spec = _deep_crash_agent(tmp_path)
    assert main(["--agent", spec, "--message", "hi", "--debug"]) == 1
    assert "Traceback (most recent call last)" in capsys.readouterr().err


def test_message_env_var_enables_traceback_without_flag(monkeypatch, tmp_path, capsys):
    # gh #83: LANGSTAGE_DEBUG=1 is honored on its own (the env path for the extension / CI
    # where argv can't be extended) — no --traceback flag needed.
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("LANGSTAGE_DEBUG", "1")
    spec = _deep_crash_agent(tmp_path)
    assert main(["--agent", spec, "--message", "hi"]) == 1
    assert "Traceback (most recent call last)" in capsys.readouterr().err


def test_message_toml_debug_true_enables_traceback(monkeypatch, tmp_path, capsys):
    # gh #90: `debug = true` in langstage.toml is the THIRD opt-in for the crash-location
    # traceback (alongside --traceback and LANGSTAGE_DEBUG=1) — the toml layer of one
    # setting on the config chain. It used to be silently ignored (only the flag/env layers
    # worked). With debug=true in the toml and NO env, NO flag, a crashing agent must now
    # show the full Python traceback on stderr — identical to the env/flag paths.
    _isolate_config(monkeypatch, tmp_path)  # strips LANGSTAGE_DEBUG; chdir + CONFIG_HOME=tmp_path
    monkeypatch.delenv("LANGSTAGE_DEBUG", raising=False)
    (tmp_path / "langstage.toml").write_text("debug = true\n")
    spec = _deep_crash_agent(tmp_path)
    assert main(["--agent", spec, "--message", "hi"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""                       # stdout stays the clean reply channel
    assert "error: KeyError" in captured.err
    assert "Traceback (most recent call last)" in captured.err
    assert "in helper" in captured.err              # names WHERE it crashed, via the toml layer


def test_message_json_mode_carries_traceback_in_error_frame(monkeypatch, tmp_path, capsys):
    # gh #83: in --json mode the traceback rides the error frame on stdout (that is the raw
    # channel), and stderr stays clean — one channel.
    _isolate_config(monkeypatch, tmp_path)
    spec = _deep_crash_agent(tmp_path)
    assert main(["--agent", spec, "--message", "hi", "--traceback", "--json"]) == 1
    captured = capsys.readouterr()
    frames = [json.loads(ln) for ln in captured.out.splitlines() if ln.strip()]
    err = next(f for f in frames if f.get("type") == "error")
    assert "in helper" in err["traceback"]
    assert captured.err == ""  # --json keeps everything on the one NDJSON channel


def test_oneshot_sink_renders_frame_traceback_only_when_present():
    # gh #83 unit: the text-mode sink renders a frame's `traceback` to stderr AFTER the
    # `error:` line, and adds nothing when the field is absent (default byte-identical).
    from langstage_vscode.sidecar import _OneShotSink

    out, err = io.StringIO(), io.StringIO()
    sink = _OneShotSink(out, err, as_json=False)
    sink.write(json.dumps({"type": "error", "error": "KeyError: 'x'",
                           "traceback": "Traceback (most recent call last):\n  ...\nKeyError"}) + "\n")
    assert out.getvalue() == ""
    assert err.getvalue().startswith("error: KeyError: 'x'\n")
    assert "Traceback (most recent call last)" in err.getvalue()

    out2, err2 = io.StringIO(), io.StringIO()
    sink2 = _OneShotSink(out2, err2, as_json=False)
    sink2.write(json.dumps({"type": "error", "error": "KeyError: 'x'"}) + "\n")
    assert err2.getvalue() == "error: KeyError: 'x'\n"  # no traceback, nothing extra


# ── gh #84: no-spec + mutex fail() paths honor the #78 front-door contract ──
#
# gh #78 routed the agent-LOAD-failure fail() to stderr in text mode but missed the two
# sibling fail() sites the same --message/--repl front doors reach FIRST: "no agent spec"
# and mutually-exclusive-flag misuse still leaked `{"type":"error",...}` to STDOUT, so a
# captured `reply=$(... --message ...)` got a JSON blob instead of a clean stdout + stderr
# diagnostic. All fail() sites now share the routing.


def test_message_no_spec_errors_on_stderr_not_stdout(monkeypatch, tmp_path, capsys):
    # gh #84: --message with no agent configured (the most common first-run mistake) ->
    # `error: no agent spec ...` on STDERR, stdout empty — not a JSON frame on stdout.
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--message", "hi"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # clean reply channel — no JSON error blob
    assert captured.err.startswith("error: ")
    assert "no agent spec" in captured.err
    assert '"type"' not in captured.out


def test_repl_no_spec_errors_on_stderr_not_stdout(monkeypatch, tmp_path, capsys):
    # gh #84: --repl shares the same no-spec front-door contract.
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi\n:quit\n"))
    assert main(["--repl"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "no agent spec" in captured.err


def test_message_mutex_misuse_errors_on_stderr_not_stdout(monkeypatch, tmp_path, capsys):
    # gh #84: --repl --message (mutually exclusive) reaches a fail() site BEFORE the load
    # path; in text mode it must route to stderr too, not leak JSON to stdout.
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--repl", "--message", "hi"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout clean
    assert captured.err.startswith("error: ")
    assert "mutually exclusive" in captured.err


def test_message_no_spec_json_keeps_frame_on_stdout(monkeypatch, tmp_path, capsys):
    # gh #84: --json is still the switch for the raw frame — a no-spec failure under --json
    # keeps the JSON error frame on stdout, stderr clean (parity with the #78 load path).
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--message", "hi", "--json"]) == 1
    captured = capsys.readouterr()
    frame = json.loads(captured.out.strip())
    assert frame["type"] == "error" and "no agent spec" in frame["error"]
    assert captured.err == ""


def test_raw_protocol_no_spec_keeps_json_frame_on_stdout(monkeypatch, tmp_path, capsys):
    # gh #84 regression guard: the raw stdio loop the extension drives (no --message/--repl)
    # is UNCHANGED — no-spec is still the stdout NDJSON `error` frame that consumer wants.
    _isolate_config(monkeypatch, tmp_path)
    assert main([]) == 1
    frame = json.loads(capsys.readouterr().out.strip())
    assert frame["type"] == "error" and "no agent spec" in frame["error"]


# ── gh #85: a checkpointer-LESS graph remembers within one process ──
#
# The README used to claim a graph compiled without a checkpointer is stateless across
# turns. It isn't: core's build_agent auto-attaches an InMemorySaver to any graph that
# lacks one, so within ONE sidecar process a checkpointer-less graph carries memory
# across turns keyed by session_id/thread_id. This pins that runtime behavior so the
# corrected README note can't silently drift back.


def _nockpt_memory_graph():
    """A graph compiled with NO checkpointer whose node counts human messages seen on the
    thread — a rising count proves the sidecar's auto-attached checkpointer carried state."""
    def respond(state):
        n = len([m for m in state["messages"] if m.__class__.__name__ == "HumanMessage"])
        return {"messages": [AIMessage(content=f"seen {n}")]}

    b = StateGraph(MessagesState)
    b.add_node("respond", respond)
    b.add_edge(START, "respond")
    b.add_edge("respond", END)
    return b.compile()  # NO checkpointer — the sidecar attaches an in-memory one


def test_checkpointerless_graph_remembers_within_one_process():
    # gh #85: same session_id, one run() process — turn 2 remembers turn 1 even though the
    # graph was compiled without a checkpointer (contradicting the old README claim).
    events = drive(_nockpt_memory_graph(), [
        {"type": "message", "session_id": "vscode", "content": "first"},
        {"type": "message", "session_id": "vscode", "content": "second"},
        {"type": "shutdown"},
    ])
    turns = _turns(events)
    assert len(turns) == 2, [e["type"] for e in events]
    assert _reply(turns[0]) == "seen 1"  # turn 1 sees only its own human message
    assert _reply(turns[1]) == "seen 2"  # turn 2 REMEMBERS turn 1 — no user checkpointer


def test_checkpointerless_graph_isolates_distinct_sessions():
    # gh #85: the auto-attached checkpointer is a genuine per-thread saver, not a global
    # buffer — a DIFFERENT session_id in the same process starts fresh at "seen 1".
    events = drive(_nockpt_memory_graph(), [
        {"type": "message", "session_id": "a", "content": "first"},
        {"type": "message", "session_id": "b", "content": "first"},
        {"type": "shutdown"},
    ])
    turns = _turns(events)
    assert _reply(turns[0]) == "seen 1"
    assert _reply(turns[1]) == "seen 1"  # session b is isolated from session a
