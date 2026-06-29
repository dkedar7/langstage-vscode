"""stdio sidecar bridging a LangGraph agent to the langstage-vscode extension.

Reads newline-delimited JSON commands on stdin, runs the agent through
``langgraph-stream-parser``, and writes newline-delimited JSON events on
stdout. The events are exactly ``event.to_dict()`` shapes — the same wire
vocabulary every other deep-agent surface (FastAPI WebSocket/SSE, Jupyter, CLI)
emits — so the TS extension's dispatcher renders them the same way.

Commands (client -> sidecar), one JSON object per line:
    {"type": "message",  "session_id": "s1", "content": "..."}
    {"type": "decision", "session_id": "s1", "decisions": [{"type": "approve"}]}
    {"type": "shutdown"}

Events (sidecar -> client), one JSON object per line:
    {"type": "ready"}                          # once, at startup
    {"type": "ack", "ref": "message|decision"} # command accepted
    <event_to_dict(...)>                       # content/tool_start/tool_end/
                                               # reasoning/extraction/interrupt
    {"type": "complete"} | {"type": "error", "error": "..."}
    {"type": "turn_end", "session_id": "s1"}   # one turn finished
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable, TextIO

from langgraph_stream_parser import (
    StreamParser,
    create_resume_input,
    event_to_dict,
    load_agent_spec,
    prepare_agent_input,
)

DEFAULT_STREAM_MODE = ["updates", "messages"]
DEFAULT_MAX_RESULT_LEN = 50_000


def run(
    graph: Any,
    stdin: Iterable[str],
    stdout: TextIO,
    *,
    stream_mode: str | list[str] = DEFAULT_STREAM_MODE,
    max_result_len: int = DEFAULT_MAX_RESULT_LEN,
) -> None:
    """Drive the command/event loop over the given streams.

    Factored out from ``main`` so it can be tested with in-memory streams and
    a fake graph. ``stdin`` is any line iterable; ``stdout`` needs ``write``.
    """
    mode = list(stream_mode) if isinstance(stream_mode, tuple) else stream_mode

    def emit(obj: dict[str, Any]) -> None:
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()

    emit({"type": "ready"})

    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            emit({"type": "error", "error": f"invalid JSON: {e}"})
            continue

        ctype = cmd.get("type")
        if ctype == "shutdown":
            break

        session_id = cmd.get("session_id", "default")
        config = {"configurable": {"thread_id": session_id}}

        if ctype == "message":
            content = cmd.get("content", "")
            if not content:
                emit({"type": "error", "error": "message requires 'content'"})
                continue
            emit({"type": "ack", "ref": "message"})
            input_data = prepare_agent_input(message=content)
        elif ctype == "decision":
            decisions = cmd.get("decisions")
            if not isinstance(decisions, list):
                emit({"type": "error", "error": "decision requires 'decisions' list"})
                continue
            emit({"type": "ack", "ref": "decision"})
            input_data = create_resume_input(decisions=decisions)
        else:
            emit({"type": "error", "error": f"unknown command type: {ctype!r}"})
            continue

        _run_turn(graph, input_data, config, mode, max_result_len, emit)
        emit({"type": "turn_end", "session_id": session_id})


def _run_turn(
    graph: Any,
    input_data: Any,
    config: dict[str, Any],
    stream_mode: str | list[str],
    max_result_len: int,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """Stream one turn; the parser emits the terminal complete/error event."""
    parser = StreamParser(stream_mode=stream_mode)
    try:
        stream = graph.stream(input_data, config=config, stream_mode=stream_mode)
        for event in parser.parse(stream):
            emit(event_to_dict(event, max_result_len=max_result_len))
    except Exception as exc:  # noqa: BLE001 — surfaced to the client as an event
        emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})


# The keyless echo agent shipped with the shared core — see `--demo`.
DEMO_AGENT_SPEC = "langgraph_stream_parser.demo.stub:graph"


def _selfcheck(spec: str, cfg: Any, *, as_json: bool) -> int:
    """Preflight the spawned interpreter + agent spec, then exit 0/non-zero.

    Answers the question the VS Code extension needs before wiring up the chat
    participant: does this interpreter have a working sidecar, and does the
    configured spec load into a *runnable* graph? Several misconfigs — most
    sharply a spec that points at a factory/module rather than the compiled
    graph — otherwise stay invisible until the first ``@langstage`` message, where
    they surface as a cryptic ``'...' object has no attribute 'stream'``. (gh #21)
    """
    import os
    import sys

    from langstage_vscode import __version__

    # Hand the agent the resolved workspace, same as the real run path.
    resolved_root = str(cfg.workspace_root)
    os.environ["LANGSTAGE_WORKSPACE_ROOT"] = resolved_root
    os.environ["DEEPAGENT_WORKSPACE_ROOT"] = resolved_root

    def verdict(ok: bool, msg: str) -> int:
        if as_json:
            sys.stdout.write(
                json.dumps({"type": "selfcheck", "ok": ok, "spec": spec, "message": msg}) + "\n"
            )
            sys.stdout.flush()
        else:
            sys.stderr.write(("OK: " if ok else "FAIL: ") + msg + "\n")
        return 0 if ok else 1

    # 1. The spec must import.
    try:
        graph = load_agent_spec(spec)
    except Exception as exc:  # noqa: BLE001 — reported as the verdict
        return verdict(False, f"agent spec {spec!r} failed to load: {type(exc).__name__}: {exc}")

    # 2. Runnable check — the sharp case: it loaded, but it isn't a CompiledGraph.
    # Name the spec and what it actually loaded instead of deferring to a
    # first-message AttributeError.
    if not callable(getattr(graph, "stream", None)):
        return verdict(
            False,
            f"agent spec {spec!r} loaded a `{type(graph).__name__}`, not a runnable graph "
            "(no `.stream`). Point the spec at the compiled graph attribute, e.g. `module:graph`.",
        )

    # 3. Drive one real turn through the actual command loop and assert the
    # documented terminal sequence (ready -> ack -> content... -> complete ->
    # turn_end), with no error frame.
    import io

    commands = [
        json.dumps({"type": "message", "content": "selfcheck ping"}),
        json.dumps({"type": "shutdown"}),
    ]
    out = io.StringIO()
    run(graph, iter(commands), out)
    frames = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    types = [f.get("type") for f in frames]

    if "error" in types:
        err = next(f.get("error") for f in frames if f.get("type") == "error")
        return verdict(False, f"agent spec {spec!r} errored driving a turn: {err}")
    if "complete" not in types or "turn_end" not in types:
        return verdict(False, f"agent spec {spec!r} did not complete a turn (frames: {types})")

    return verdict(
        True,
        f"langstage-vscode {__version__} - {spec} drove a turn; interpreter has a working sidecar.",
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys

    from langgraph_stream_parser.host import HostConfig

    parser = argparse.ArgumentParser(prog="langstage-vscode-sidecar")
    parser.add_argument(
        "--agent",
        default=None,
        help="Agent spec 'path.py:var' or 'module:var' "
        "(overrides LANGSTAGE_AGENT_SPEC / langstage.toml [agent].spec).",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace root (overrides LANGSTAGE_WORKSPACE_ROOT / langstage.toml).",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run with the built-in keyless demo agent (no API key needed).",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print the resolved configuration (defaults < langstage.toml < env < CLI) and exit.",
    )
    parser.add_argument(
        "--selfcheck",
        "--smoke",
        action="store_true",
        dest="selfcheck",
        help="Preflight: load the configured agent (or the demo stub), assert it is a runnable "
        "graph, drive one turn, and exit 0 (healthy) / non-zero. Does not enter the command loop.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --selfcheck, emit a machine-readable JSON verdict instead of a human-readable line.",
    )
    from langstage_vscode import __version__

    parser.add_argument(
        "--version",
        action="version",
        version=f"langstage-vscode-sidecar {__version__}",
        help="Print the version and exit.",
    )
    args = parser.parse_args(argv)

    # Same resolution chain as every other deep-agent surface:
    # defaults < langstage.toml < LANGSTAGE_* env < CLI flags.
    cfg = HostConfig.resolve(
        overrides={"agent_spec": args.agent, "workspace_root": args.workspace}
    )

    if args.show_config:
        # This is a pure stdio sidecar — it never opens a socket or renders a
        # UI, so the inherited host/port/debug/title keys do nothing here.
        # Hide them so --show-config only advertises what the sidecar honors
        # (agent_spec, workspace_root). (gh #14)
        print(cfg.describe(omit_keys=["host", "port", "debug", "title"]))
        return 0

    def fail(msg: str) -> int:
        sys.stdout.write(json.dumps({"type": "error", "error": msg}) + "\n")
        sys.stdout.flush()
        return 1

    spec = cfg.agent_spec
    if args.demo:
        if args.agent:
            return fail("--demo and --agent are mutually exclusive")
        spec = DEMO_AGENT_SPEC

    if args.selfcheck:
        # With no agent configured, validate the runtime itself via the demo stub.
        return _selfcheck(spec or DEMO_AGENT_SPEC, cfg, as_json=args.json)

    if not spec:
        return fail(
            "no agent spec (pass --agent or --demo, set LANGSTAGE_AGENT_SPEC, "
            "or set [agent].spec in langstage.toml)"
        )

    # Hand the RESOLVED workspace to the agent. cfg.workspace_root already applied
    # precedence (CLI --workspace > env > toml), so it is authoritative — assign it,
    # don't setdefault. setdefault was a no-op when LANGSTAGE_WORKSPACE_ROOT was
    # already exported, so the agent read the stale env value while --show-config
    # reported the --workspace override as winning. Keep the legacy name in sync so
    # an agent reading the deprecated var doesn't get a stale directory. (gh #19)
    resolved_root = str(cfg.workspace_root)
    os.environ["LANGSTAGE_WORKSPACE_ROOT"] = resolved_root
    os.environ["DEEPAGENT_WORKSPACE_ROOT"] = resolved_root
    try:
        graph = load_agent_spec(spec)
    except Exception as exc:  # noqa: BLE001
        return fail(f"failed to load agent {spec!r}: {type(exc).__name__}: {exc}")

    run(graph, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
