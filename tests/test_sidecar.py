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

from langgraph.graph import END, START, MessagesState, StateGraph

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


# ── main(): config resolution + --demo / --show-config ───────────────


def _isolate_config(monkeypatch, tmp_path):
    """Point cwd + the global config home at an empty tmp dir and strip
    legacy + canonical env so main() resolves from pure defaults."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LANGSTAGE_CONFIG_HOME", str(tmp_path))
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT", "DEEPAGENT_WORKSPACE_ROOT"):
        monkeypatch.delenv(var, raising=False)


def test_main_show_config(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--show-config"]) == 0
    assert "LANGSTAGE_AGENT_SPEC" in capsys.readouterr().out


def test_main_accepts_short_agent_flag(monkeypatch, tmp_path, capsys):
    # gh dogfood-F9: cli uses `-a`; the sidecar accepts it too (was --agent only), so
    # the same spec + flag work across surfaces.
    _isolate_config(monkeypatch, tmp_path)
    assert main(["-a", "langstage_core.demo.stub:graph", "--show-config"]) == 0
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
