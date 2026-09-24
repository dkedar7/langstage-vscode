"""Regression tests for the reports fixed by adopting langstage-core 1.0.36 (Wave 2).

Each test is built from its issue's repro and driven through the sidecar's own entry
points (``main()`` / ``run()`` / a ``python -m langstage_vscode`` subprocess), so it
pins the fix on the vscode path rather than in core alone. Most of these fixes live
in core; the sidecar changes are the spec ``base_dir`` (replacing the deleted
``_absolutize_spec_path``), ``[configurable]`` forwarding, the ``message_id``
paragraph break in the ``--message`` / ``--repl`` assembler, and routing console
output through ``langstage_core.console.safe_write``.
"""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from langstage_vscode.sidecar import main, run

# A keyless one-node agent that replies with a fixed string.
_AGENT_SRC = (
    "from langgraph.graph import StateGraph, START, END, MessagesState\n"
    "from langchain_core.messages import AIMessage\n"
    "def r(state):\n"
    "    return {'messages': [AIMessage(content=%r)]}\n"
    "_b = StateGraph(MessagesState)\n"
    "_b.add_node('r', r); _b.add_edge(START, 'r'); _b.add_edge('r', END)\n"
    "graph = _b.compile()\n"
)


def _write_agent(path: Path, reply: str = "hi from agent") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_AGENT_SRC % reply, encoding="utf-8")
    return path


def _isolate(monkeypatch, tmp_path, *, home: Path | None = None) -> Path:
    """Empty global config home, no LANGSTAGE_*/DEEPAGENT_* env, cwd = tmp_path."""
    monkeypatch.chdir(tmp_path)
    gh = tmp_path / "_global"
    gh.mkdir(exist_ok=True)
    monkeypatch.setenv("LANGSTAGE_CONFIG_HOME", str(gh))
    for var in ("LANGSTAGE_AGENT_SPEC", "DEEPAGENT_AGENT_SPEC", "LANGSTAGE_WORKSPACE_ROOT",
                "DEEPAGENT_WORKSPACE_ROOT", "LANGSTAGE_DEBUG", "DEEPAGENT_DEBUG",
                "DEEPAGENTS_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    if home is not None:
        home.mkdir(parents=True, exist_ok=True)
        # os.path.expanduser reads USERPROFILE on Windows and HOME elsewhere.
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
    return gh


def _frames(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


# ── gh #135: the agent spec is whitespace-stripped ──────────────────────────


@pytest.mark.parametrize("spec", ["agent.py:graph ", " agent.py:graph", "agent.py : graph"])
def test_135_stray_whitespace_in_spec_still_loads(monkeypatch, tmp_path, capsys, spec):
    _isolate(monkeypatch, tmp_path)
    _write_agent(tmp_path / "agent.py", "stripped ok")
    assert main(["--agent", spec, "--message", "hi"]) == 0, capsys.readouterr().err
    assert capsys.readouterr().out.strip() == "stripped ok"


def test_135_selfcheck_with_trailing_space_is_healthy(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _write_agent(tmp_path / "agent.py")
    rc = main(["--agent", "agent.py:graph ", "--selfcheck"])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "has no attribute 'graph '" not in err


# ── gh #125: a leading `~` expands in the spec and the workspace root ───────


def test_125_tilde_agent_spec_loads_from_home(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    _isolate(monkeypatch, tmp_path, home=home)
    _write_agent(home / "tilde_agents" / "agent.py", "loaded from home")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    rc = main(["--agent", "~/tilde_agents/agent.py:graph", "--message", "hi"])
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert out.strip() == "loaded from home"
    assert not (project / "~").exists()


def test_125_tilde_selfcheck_and_env_spec(monkeypatch, tmp_path, capsys):
    # The same `~` via LANGSTAGE_AGENT_SPEC (never shell-expanded) on the preflight verb.
    home = tmp_path / "home"
    _isolate(monkeypatch, tmp_path, home=home)
    _write_agent(home / "tilde_agents" / "agent.py", "loaded from home")
    monkeypatch.setenv("LANGSTAGE_AGENT_SPEC", "~/tilde_agents/agent.py:graph")
    assert main(["--selfcheck"]) == 0, capsys.readouterr().err
    assert not (tmp_path / "~").exists()


def test_125_tilde_workspace_root_in_toml_expands_not_a_literal_dir(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    _isolate(monkeypatch, tmp_path, home=home)
    agent = _write_agent(tmp_path / "agent.py")
    project = tmp_path / "project2"
    project.mkdir()
    (project / "langstage.toml").write_text('[workspace]\nroot = "~/my_workspace"\n')
    monkeypatch.chdir(project)
    rc = main(["--agent", f"{agent}:graph", "--message", "hi"])
    assert rc == 0, capsys.readouterr().err
    assert not (project / "~").exists()           # no junk literal `~` dir in the project
    assert (home / "my_workspace").is_dir()        # the real home-relative workspace
    assert Path(os.getcwd()).resolve() == (home / "my_workspace").resolve()


# ── gh #123: a relative [agent] spec resolves against its langstage.toml ────


def test_123_relative_toml_spec_loads_from_a_subdirectory(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    proj = tmp_path / "myproj"
    _write_agent(proj / "agent.py", "hi from agent")
    (proj / "langstage.toml").write_text('[agent]\nspec = "agent.py:graph"\n')
    (proj / "src").mkdir()
    monkeypatch.chdir(proj / "src")

    assert main(["--selfcheck"]) == 0, capsys.readouterr().err
    capsys.readouterr()
    assert main(["--message", "hi"]) == 0
    assert capsys.readouterr().out.strip() == "hi from agent"
    # --show-config reports the path it will actually load (absolute, under the toml dir)
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    spec_val = obj["config"]["agent_spec"]["value"]
    assert Path(spec_val.rpartition(":")[0]).resolve() == (proj / "agent.py").resolve()


def test_123_dotted_toml_spec_resolves_from_the_toml_dir(monkeypatch, tmp_path, capsys):
    # The dotted twin: core can't rebase `pkg.mod:attr` in the string, so the sidecar
    # passes the toml's directory as base_dir (cfg.toml_dir_for). Run from a subdir
    # with the workspace elsewhere, so neither cwd could have found the package.
    _isolate(monkeypatch, tmp_path)
    proj = tmp_path / "dotproj"
    pkg = proj / "wave2_toml_pkg"
    _write_agent(pkg / "agent.py", "dotted from toml")
    (pkg / "__init__.py").write_text("")
    (proj / "langstage.toml").write_text('[agent]\nspec = "wave2_toml_pkg.agent:graph"\n')
    (proj / "src").mkdir()
    ws = tmp_path / "ws"
    monkeypatch.chdir(proj / "src")
    assert main(["--workspace", str(ws), "--message", "hi"]) == 0, capsys.readouterr().err
    assert capsys.readouterr().out.strip() == "dotted from toml"


def test_relative_agent_flag_and_dotted_flag_resolve_from_launch_cwd(monkeypatch, tmp_path, capsys):
    # The behavior the deleted _absolutize_spec_path carried (gh #88 / cli gh #30): the
    # sidecar chdirs into the workspace before import, yet a relative --agent (file or
    # dotted) still resolves from where it was typed, via base_dir=<launch cwd>.
    # The dotted case runs first, from a directory no file-spec load has put on sys.path.
    _isolate(monkeypatch, tmp_path)
    launch = tmp_path / "launch"
    pkg = launch / "wave2_flag_pkg"
    _write_agent(pkg / "agent.py", "relative dotted")
    (pkg / "__init__.py").write_text("")
    _write_agent(launch / "agent.py", "relative file")
    ws = tmp_path / "elsewhere"
    monkeypatch.chdir(launch)
    assert main(["--agent", "wave2_flag_pkg.agent:graph", "--workspace", str(ws),
                 "--message", "hi"]) == 0, capsys.readouterr().err
    assert capsys.readouterr().out.strip() == "relative dotted"
    os.chdir(launch)
    assert main(["--agent", "./agent.py:graph", "--workspace", str(ws), "--message", "hi"]) == 0
    assert capsys.readouterr().out.strip() == "relative file"


# ── gh #126: a relative [workspace] root resolves against its langstage.toml ─


def test_126_relative_toml_workspace_root_anchors_to_toml_dir(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    agent = _write_agent(tmp_path / "isoagent.py")
    proj = tmp_path / "proj"
    (proj / "src" / "deep").mkdir(parents=True)
    (proj / "langstage.toml").write_text(
        f'[agent]\nspec = "{agent.as_posix()}:graph"\n[workspace]\nroot = "wsdir"\n'
    )
    monkeypatch.chdir(proj / "src" / "deep")
    assert main(["--selfcheck"]) == 0, capsys.readouterr().err
    assert (proj / "wsdir").is_dir()
    assert not (proj / "src" / "deep" / "wsdir").exists()


# ── gh #127: [configurable] reaches the graph and --show-config ─────────────

_CONFIGURABLE_AGENT = (
    "from langgraph.graph import StateGraph, START, END, MessagesState\n"
    "from langchain_core.messages import AIMessage\n"
    "from langchain_core.runnables import RunnableConfig\n"
    "def respond(state: MessagesState, config: RunnableConfig):\n"
    "    val = (config or {}).get('configurable', {}).get('my_key', '<MISSING>')\n"
    "    return {'messages': [AIMessage(content=f'my_key={val}')]}\n"
    "_b = StateGraph(MessagesState); _b.add_node('respond', respond)\n"
    "_b.add_edge(START, 'respond'); _b.add_edge('respond', END)\n"
    "graph = _b.compile()\n"
)


def _configurable_project(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(_CONFIGURABLE_AGENT)
    (tmp_path / "langstage.toml").write_text(
        '[agent]\nspec = "a.py:graph"\n\n[configurable]\nmy_key = "HELLO"\n'
    )


def test_127_configurable_is_forwarded_to_the_graph(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _configurable_project(tmp_path)
    assert main(["--message", "hi"]) == 0, capsys.readouterr().err
    assert capsys.readouterr().out.strip() == "my_key=HELLO"


def test_127_configurable_on_the_raw_stdio_path_keeps_sessions_apart(tmp_path):
    # The extension's path: run() with the table; each session still gets its own thread.
    import importlib.util

    (tmp_path / "a.py").write_text(_CONFIGURABLE_AGENT)
    spec = importlib.util.spec_from_file_location("_cfg_agent_127", tmp_path / "a.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stdin = io.StringIO(
        json.dumps({"type": "message", "session_id": "s1", "content": "hi"}) + "\n"
        + json.dumps({"type": "shutdown"}) + "\n"
    )
    out = io.StringIO()
    run(mod.graph, stdin, out, configurable={"my_key": "FROM_TOML", "thread_id": "hijack"})
    frames = _frames(out.getvalue())
    assert "".join(f.get("content", "") for f in frames if f["type"] == "content") == "my_key=FROM_TOML"
    assert frames[-1] == {"type": "turn_end", "session_id": "s1"}


def test_127_show_config_renders_configurable(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _configurable_project(tmp_path)
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    assert obj["configurable"] == {"my_key": "HELLO"}
    assert main(["--show-config"]) == 0
    text = capsys.readouterr().out
    assert "my_key" in text and "HELLO" in text


# ── gh #110: a malformed langstage.toml is reported, not "absent" ───────────


def test_110_malformed_toml_is_reported_as_malformed(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    bad = tmp_path / "langstage.toml"
    bad.write_text('[agent]\nspec = "a.py:graph"\n[agent]\nspec = "b.py:graph"\n')
    assert main(["--show-config", "--json"]) == 0
    toml = json.loads(capsys.readouterr().out)["toml"]
    assert toml["found"] is True and toml["malformed"] is True
    assert Path(toml["path"]).resolve() == bad.resolve()
    assert [Path(m["path"]).resolve() for m in toml["malformed_files"]] == [bad.resolve()]
    assert "agent" in toml["malformed_files"][0]["error"]
    assert main(["--show-config"]) == 0
    text = capsys.readouterr().out
    assert "MALFORMED" in text
    assert "no langstage.toml" not in text


# ── gh #107: toml.paths lists the global file too ───────────────────────────


def test_107_toml_paths_lists_global_and_project(monkeypatch, tmp_path, capsys):
    gh = _isolate(monkeypatch, tmp_path)
    (gh / "config.toml").write_text('[workspace]\nroot = "/from/global"\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "langstage.toml").write_text('[agent]\nspec = "my_agent.py:graph"\n')
    monkeypatch.chdir(proj)
    assert main(["--show-config", "--json"]) == 0
    obj = json.loads(capsys.readouterr().out)
    paths = [Path(p).resolve() for p in obj["toml"]["paths"]]
    assert paths == [(gh / "config.toml").resolve(), (proj / "langstage.toml").resolve()]
    # The value's source names a file that IS in toml.paths (the JSON is consistent).
    assert "config.toml" in obj["config"]["workspace_root"]["source"]


# ── gh #112: a legacy DEEPAGENT_* env var is announced once ─────────────────


def test_112_legacy_env_var_announced_exactly_once(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("LANGSTAGE_SUPPRESS_LEGACY_NOTICE", "PYTHONWARNINGS",
                        "LANGSTAGE_AGENT_SPEC",
                        # core mutes legacy notices under pytest; this child is the real CLI
                        "PYTEST_CURRENT_TEST")}
    env["DEEPAGENT_AGENT_SPEC"] = "x.py:graph"
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--show-config"],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("DEEPAGENT_AGENT_SPEC is deprecated") == 1, proc.stderr
    assert "DeprecationWarning" not in proc.stderr
    assert "note:" in proc.stderr


# ── gh #114: allowed_decisions reflects the interrupt's own config ──────────

_HITL_SRC = (
    "from langgraph.graph import StateGraph, START, END\n"
    "from langgraph.graph.message import add_messages\n"
    "from langgraph.types import interrupt\n"
    "from typing import Annotated\n"
    "from typing_extensions import TypedDict\n"
    "class S(TypedDict):\n"
    "    messages: Annotated[list, add_messages]\n"
    "def node(state: S):\n"
    "    interrupt([{\n"
    "        'action_request': {'action': 'delete_database', 'args': {'name': 'prod'}},\n"
    "        'config': %r,\n"
    "        'description': 'Approve deleting the prod database?',\n"
    "    }])\n"
    "    return {'messages': []}\n"
    "g = StateGraph(S); g.add_node('respond', node)\n"
    "g.add_edge(START, 'respond'); g.add_edge('respond', END)\n"
    "graph = g.compile()\n"
)


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"allow_accept": True, "allow_edit": False, "allow_respond": False,
          "allow_ignore": False}, {"approve"}),
        ({"allow_accept": True, "allow_edit": False, "allow_respond": False,
          "allow_ignore": True}, {"reject", "approve"}),
    ],
)
def test_114_interrupt_advertises_only_its_own_verbs(monkeypatch, tmp_path, capsys, config, expected):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "hitl_repro.py").write_text(_HITL_SRC % config)
    rc = main(["--agent", "./hitl_repro.py:graph", "--message", "go", "--json"])
    assert rc == 2
    interrupt = next(f for f in _frames(capsys.readouterr().out) if f["type"] == "interrupt")
    assert set(interrupt["allowed_decisions"]) == expected
    # ...and the human notice offers only those verbs.
    assert main(["--agent", "./hitl_repro.py:graph", "--message", "go"]) == 2
    err = capsys.readouterr().err
    allowed_line = next(ln for ln in err.splitlines() if "allowed:" in ln)
    assert set(allowed_line.split("allowed:")[1].strip().split(" | ")) == expected
    assert "edit" not in allowed_line


# ── gh #103: a HITL resume logs no ag-ui deprecation warning ────────────────


def test_103_repl_resume_leaves_stderr_free_of_adapter_warnings(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LANGSTAGE_", "DEEPAGENT"))}
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--demo=tools", "--repl"],
        input="ask me first\napprove\n:quit\n",
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "interrupt: agent paused" in proc.stderr       # the resume really happened
    assert "Resumed." in proc.stdout
    assert "deprecated" not in proc.stderr, proc.stderr
    assert "failed to parse" not in proc.stderr, proc.stderr


# ── gh #108: distinct AIMessages get a message boundary ─────────────────────

_MULTINODE_SRC = (
    "from langgraph.graph import StateGraph, START, END\n"
    "from langgraph.graph.message import add_messages\n"
    "from langchain_core.messages import AIMessage\n"
    "from typing import Annotated\n"
    "from typing_extensions import TypedDict\n"
    "class State(TypedDict):\n"
    "    messages: Annotated[list, add_messages]\n"
    "def plan(state):   return {'messages': [AIMessage(content='PLAN: I will answer in two parts.')]}\n"
    "def answer(state): return {'messages': [AIMessage(content='ANSWER: the sky is blue.')]}\n"
    "_b = StateGraph(State)\n"
    "_b.add_node('plan', plan); _b.add_node('answer', answer)\n"
    "_b.add_edge(START, 'plan'); _b.add_edge('plan', 'answer'); _b.add_edge('answer', END)\n"
    "graph = _b.compile()\n"
)


def test_108_message_text_separates_distinct_messages(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "multinode.py").write_text(_MULTINODE_SRC)
    assert main(["--agent", "./multinode.py:graph", "--message", "hi"]) == 0
    assert capsys.readouterr().out == (
        "PLAN: I will answer in two parts.\n\nANSWER: the sky is blue.\n"
    )


def test_108_raw_frames_carry_a_changing_message_id(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "multinode.py").write_text(_MULTINODE_SRC)
    assert main(["--agent", "./multinode.py:graph", "--message", "hi", "--json"]) == 0
    content = [f for f in _frames(capsys.readouterr().out) if f["type"] == "content"]
    assert [f["node"] for f in content] == ["plan", "answer"]
    ids = [f["message_id"] for f in content]
    assert all(isinstance(i, str) and i for i in ids) and ids[0] != ids[1]


def test_108_repl_boundary_resets_per_turn(monkeypatch, tmp_path, capsys):
    # One message per turn must not gain a leading break on the next turn.
    from langstage_vscode.sidecar import _run_repl

    _isolate(monkeypatch, tmp_path)
    graph = __import__("langstage_core").load_agent_spec(
        str(_write_agent(tmp_path / "one.py", "solo")) + ":graph")
    out, err = io.StringIO(), io.StringIO()
    assert _run_repl(graph, spec=None, as_json=False, stdin=iter(["a\n", "b\n"]),
                     stdout=out, stderr=err, show_prompt=False) == 0
    assert out.getvalue() == "solo\nsolo\n"


# ── gh #105: content from an earlier node survives a later node's error ─────

_PARTIAL_SRC = (
    "from typing import Annotated\n"
    "from typing_extensions import TypedDict\n"
    "from langgraph.graph import StateGraph, START, END\n"
    "from langgraph.graph.message import add_messages\n"
    "from langchain_core.messages import AIMessage\n"
    "class State(TypedDict):\n"
    "    messages: Annotated[list, add_messages]\n"
    "def answer(state): return {'messages': [AIMessage(content='Here is my partial answer.')]}\n"
    "def use_tool(state): raise RuntimeError('tool call failed')\n"
    "b = StateGraph(State)\n"
    "b.add_node('answer', answer); b.add_node('use_tool', use_tool)\n"
    "b.add_edge(START, 'answer'); b.add_edge('answer', 'use_tool'); b.add_edge('use_tool', END)\n"
    "graph = b.compile()\n"
)


def test_105_partial_reply_is_emitted_before_the_error(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "agent.py").write_text(_PARTIAL_SRC)
    assert main(["--agent", "./agent.py:graph", "--message", "hi"]) == 1
    out, err = capsys.readouterr()
    assert out.strip() == "Here is my partial answer."
    assert "error: RuntimeError: tool call failed" in err

    assert main(["--agent", "./agent.py:graph", "--message", "hi", "--json"]) == 1
    types = [(f["type"], f.get("content")) for f in _frames(capsys.readouterr().out)]
    i_content = types.index(("content", "Here is my partial answer."))
    i_error = [t for t, _ in types].index("error")
    assert i_content < i_error


# ── console output goes through core's safe_write ───────────────────────────


def test_message_reply_survives_cp1252_stdout_via_core_safe_write(tmp_path):
    agent = _write_agent(tmp_path / "agent.py", "reply 日本")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGSTAGE_", "DEEPAGENT"))}
    env["PYTHONIOENCODING"] = "cp1252"
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--agent", f"{agent}:graph", "--message", "hi"],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "UnicodeEncodeError" not in proc.stderr
    assert "\\u65e5\\u672c" in proc.stdout


def test_local_write_safe_helper_is_gone():
    import langstage_vscode.sidecar as sc

    assert not hasattr(sc, "_write_safe")
    assert not hasattr(sc, "_absolutize_spec_path")
