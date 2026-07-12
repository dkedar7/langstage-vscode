"""stdio sidecar bridging a LangGraph agent to the langstage-vscode extension.

Reads newline-delimited JSON commands on stdin, runs the agent through
``langstage-core``'s in-process AG-UI adapter, and writes newline-delimited JSON
events on stdout. The events are exactly ``event_to_dict()`` shapes — the same
wire vocabulary every other LangStage surface (web, Jupyter, CLI) emits — so the
TS extension's dispatcher renders them the same way.

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
import os
from typing import Any, Callable, Iterable, TextIO

from langstage_core import apply_workspace, load_agent_spec, workspace_root

DEFAULT_MAX_RESULT_LEN = 50_000


def _write_safe(stream: TextIO, text: str) -> None:
    """Write ``text`` to ``stream``, degrading any character the stream's encoding
    can't represent to a backslash escape instead of crashing.

    The VS Code extension spawns the sidecar over a cp1252 pipe (and a Western-
    Windows console is cp1252 too), both with the ``strict`` error handler — so a
    raw ``print`` of text carrying a non-Latin-1 character (an emoji/CJK char an LLM
    emits routinely; a CJK/Cyrillic agent spec or project path) dies with an
    uncaught ``UnicodeEncodeError`` and emits nothing. Every human-readable text
    path routes through here so it degrades gracefully; the JSON protocol path is
    already ASCII-safe via ``ensure_ascii``. Full fidelity is preserved on a UTF-8
    stream. Shared by ``--show-config`` (gh #42) and one-shot ``--message`` (gh #51)
    so the two guards can't drift again — the "one shared helper" rationale gh #46
    used for ``_runnable_graph_error``.
    """
    enc = getattr(stream, "encoding", None) or "utf-8"
    stream.write(text.encode(enc, "backslashreplace").decode(enc, "replace"))


def _runnable_graph_error(graph: Any, spec: str | None = None) -> str | None:
    """Return an actionable message if ``graph`` isn't a runnable CompiledGraph, else None.

    The single source of the "not a runnable graph" message, shared by ``--selfcheck``
    (preflight) and ``run()`` (the actual runtime path) so the two can't drift (gh #46).
    A runnable graph exposes a callable ``.stream``; a factory function, a StateGraph that
    was never compiled, or any non-graph value (``42``) does not.
    """
    if callable(getattr(graph, "stream", None)):
        return None
    label = f"agent spec {spec!r} " if spec else "the agent "
    return (
        f"{label}loaded a `{type(graph).__name__}`, not a runnable graph "
        "(no `.stream`). Point the spec at the compiled graph attribute, e.g. `module:graph`."
    )


def run(
    graph: Any,
    stdin: Iterable[str],
    stdout: TextIO,
    *,
    spec: str | None = None,
    max_result_len: int = DEFAULT_MAX_RESULT_LEN,
    **_legacy: Any,  # accepts + ignores the removed stream_mode/agui kwargs
) -> None:
    """Drive the command/event loop over the given streams.

    Since langstage-core 1.0 (ADR 0003) turns stream through the in-process AG-UI
    adapter — the only path — emitting ``event_to_dict``-shaped frames so the TS
    extension is unchanged. ``stdin`` is any line iterable; ``stdout`` needs ``write``.
    """

    def emit(obj: dict[str, Any]) -> None:
        stdout.write(json.dumps(obj) + "\n")
        stdout.flush()

    emit({"type": "ready"})

    # The spec loaded, but if it isn't a runnable graph (a factory function, an
    # uncompiled StateGraph, a bare value), core's build_agent blows up deep inside
    # ag-ui with a raw `AttributeError: 'function' object has no attribute 'nodes'`
    # that ISN'T a RuntimeError — so it used to escape the handler below and kill the
    # process right after `ready`, with no protocol `error` frame (gh #46). Guard here
    # with the same actionable message `--selfcheck` gives, so the runtime path
    # degrades to a frame instead of a crash on this documented footgun.
    graph_error = _runnable_graph_error(graph, spec)
    if graph_error is not None:
        emit({"type": "error", "error": graph_error})
        return

    from .agui_stream import build_session_agent

    try:
        agui_agent = build_session_agent(graph)
    except Exception as exc:  # noqa: BLE001 — any build failure becomes an error frame, not a crash
        emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        return

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

        agui_message: str | None = None
        agui_resume: Any = None
        if ctype == "message":
            content = cmd.get("content", "")
            if not content:
                emit({"type": "error", "error": "message requires 'content'"})
                continue
            emit({"type": "ack", "ref": "message"})
            agui_message = content
        elif ctype == "decision":
            decisions = cmd.get("decisions")
            # An empty list is as invalid as a non-list: there's no interrupt to
            # resume, so it must error, not ack + drive a spurious turn — mirroring
            # the `message` path's empty-content rejection above. (gh #33)
            if not isinstance(decisions, list) or not decisions:
                emit({"type": "error", "error": "decision requires a non-empty 'decisions' list"})
                continue
            emit({"type": "ack", "ref": "decision"})
            agui_resume = {"decisions": decisions}
        else:
            emit({"type": "error", "error": f"unknown command type: {ctype!r}"})
            continue

        _run_turn_agui(
            agui_agent, agui_message, agui_resume, config, max_result_len, emit
        )
        emit({"type": "turn_end", "session_id": session_id})


def _run_turn_agui(
    agent: Any,
    message: str | None,
    resume: Any,
    config: dict[str, Any],
    max_result_len: int,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """Stream one turn through the in-process AG-UI adapter, emitting
    ``event_to_dict``-shaped frames (the sidecar's only streaming path, ADR 0003)."""
    from .agui_stream import stream_events_sync

    thread_id = config.get("configurable", {}).get("thread_id", "default")
    try:
        for frame in stream_events_sync(
            agent,
            message or "",
            thread_id,
            resume=resume,
            max_result_len=max_result_len,
        ):
            emit(frame)
    except Exception as exc:  # noqa: BLE001 — surfaced to the client as an event
        emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})


# The keyless echo agent shipped with the shared core — see `--demo`.
DEMO_AGENT_SPEC = "langstage_core.demo.stub:graph"


def _selfcheck(spec: str, cfg: Any, *, as_json: bool) -> int:
    """Preflight the spawned interpreter + agent spec, then exit 0/non-zero.

    Answers the question the VS Code extension needs before wiring up the chat
    participant: does this interpreter have a working sidecar, and does the
    configured spec load into a *runnable* graph? Several misconfigs — most
    sharply a spec that points at a factory/module rather than the compiled
    graph — otherwise stay invisible until the first ``@langstage`` message, where
    they surface as a cryptic ``'...' object has no attribute 'stream'``. (gh #21)
    """
    import sys

    from langstage_vscode import __version__

    # Hand the agent the resolved workspace, same as the real run path.
    apply_workspace(cfg.workspace_root)

    def verdict(ok: bool, msg: str) -> int:
        if as_json:
            sys.stdout.write(
                json.dumps(
                    {"type": "selfcheck", "ok": ok, "spec": spec, "message": msg}
                )
                + "\n"
            )
            sys.stdout.flush()
        else:
            sys.stderr.write(("OK: " if ok else "FAIL: ") + msg + "\n")
        return 0 if ok else 1

    # 1. The spec must import.
    try:
        graph = load_agent_spec(spec)
    except Exception as exc:  # noqa: BLE001 — reported as the verdict
        return verdict(
            False, f"agent spec {spec!r} failed to load: {type(exc).__name__}: {exc}"
        )

    # 2. Runnable check — the sharp case: it loaded, but it isn't a CompiledGraph.
    # Name the spec and what it actually loaded instead of deferring to a
    # first-message AttributeError. Shares the message with the runtime path (gh #46).
    runnable_error = _runnable_graph_error(graph, spec)
    if runnable_error is not None:
        return verdict(False, runnable_error)

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
        return verdict(
            False, f"agent spec {spec!r} did not complete a turn (frames: {types})"
        )

    return verdict(
        True,
        f"langstage-vscode {__version__} - {spec} drove a turn; interpreter has a working sidecar.",
    )


class _OneShotSink:
    """A write-only NDJSON sink handed to ``run()`` in place of an in-memory
    ``StringIO`` buffer, so a one-shot ``--message`` turn streams to the real stdout
    frame-by-frame instead of buffering the whole turn and dumping it at the end
    (gh #50).

    ``run()`` writes one ``event_to_dict`` frame per line (and ``flush()``es after
    each); this sink forwards them the instant they arrive:

    - ``--json``: write every frame to stdout as it's emitted (error frames included,
      matching the buffered contract) — a genuine streaming NDJSON source, so
      ``... --message x --json | jq -c .`` sees frames live instead of all at once.
    - human mode: print each ``content`` frame's text as it arrives (the reply types
      out live, with no trailing newline until the turn ends), routing ``error``
      frames to stderr and keeping stdout the clean reply channel.

    Either way it records whether any ``error`` frame appeared, for the ``0`` clean /
    non-zero on error exit contract ``--message`` has carried since gh #48. Every
    write goes through ``_write_safe``, so a non-Latin-1 char in the reply still
    degrades to an escape on a cp1252 stdout instead of crashing (gh #51 holds after
    the switch to streaming).
    """

    def __init__(self, out: TextIO, err: TextIO, *, as_json: bool) -> None:
        self._out = out
        self._err = err
        self._as_json = as_json
        self._buf = ""
        self.saw_error = False
        self.printed_reply = False

    def write(self, s: str) -> None:
        # run()'s emit() writes each frame as a single "<json>\n", but buffer and
        # split on newlines so a coalesced or partial write can't split a frame.
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                self._on_frame(line)

    def flush(self) -> None:
        self._out.flush()

    def _on_frame(self, line: str) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            return
        ftype = frame.get("type")
        if ftype == "error":
            self.saw_error = True

        if self._as_json:
            # Forward the frame verbatim (run() emitted it via json.dumps, so it's
            # already ASCII-safe), flushing so a piped consumer sees it immediately.
            _write_safe(self._out, line + "\n")
            self._out.flush()
            return

        if ftype == "content":
            text = frame.get("content", "")
            if text:
                _write_safe(self._out, text)
                self._out.flush()
                self.printed_reply = True
        elif ftype == "error":
            # stdout stays the clean reply channel; surface the failure on stderr.
            _write_safe(self._err, f"error: {frame.get('error', '')}\n")

    def close_reply(self) -> None:
        """Terminate a live human-mode reply with the trailing newline the old
        ``print(reply)`` added, so the shell prompt returns on its own line. No-op in
        ``--json`` mode and when the turn produced no ``content``."""
        if self.printed_reply:
            _write_safe(self._out, "\n")
            self._out.flush()


def _run_once(graph: Any, message: str, *, spec: str | None, as_json: bool) -> int:
    """Drive exactly ONE turn with ``message`` and stream the result, then exit — no
    ``shutdown`` handshake required from the caller (gh #48).

    Human mode types out the assistant reply (the ``content`` frames) as it's
    produced; ``--json`` streams the raw ``event_to_dict`` frames (one per line) for
    scripting. Both stream frame-by-frame as ``run()`` emits them rather than
    buffering the whole turn and dumping it at the end (gh #50) — so a slow or
    token-streaming agent no longer looks frozen. Exit 0 on a clean turn, non-zero if
    an ``error`` frame appears — the same contract the stdio loop's ``error`` frame
    carries. Internally this is the ``run()`` loop fed an in-memory
    ``[message, shutdown]`` script (like ``_selfcheck``), but writing to a streaming
    sink instead of a ``StringIO`` buffer.
    """
    import sys

    commands = [
        json.dumps({"type": "message", "session_id": "once", "content": message}),
        json.dumps({"type": "shutdown"}),
    ]
    sink = _OneShotSink(sys.stdout, sys.stderr, as_json=as_json)
    run(graph, iter(commands), sink, spec=spec)
    sink.close_reply()  # human mode: close the streamed reply with a trailing newline

    return 1 if sink.saw_error else 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from langstage_core.host import HostConfig

    parser = argparse.ArgumentParser(prog="langstage-vscode-sidecar")
    parser.add_argument(
        "-a",
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
        "--message",
        "--prompt",
        default=None,
        dest="message",
        metavar="TEXT",
        help="One-shot: drive a single turn with TEXT, print the agent's reply, and exit "
        "(0 on success / non-zero on an error frame) with no stdio protocol handshake. "
        "Pair with --json to emit the raw event frames instead of the assembled text.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --selfcheck, emit a machine-readable JSON verdict; with --message, emit the "
        "raw event_to_dict frames (one per line) instead of the assembled reply text.",
    )
    from langstage_vscode import __version__

    parser.add_argument(
        "--version",
        action="version",
        version=f"langstage-vscode-sidecar {__version__}",
        help="Print the version and exit.",
    )
    # Removed in 0.5.0 (AG-UI is the only streaming path now, ADR 0003), but the
    # 0.5.0 CHANGELOG promised --agui would be *accepted-and-ignored* so existing
    # launch configs carrying the old opt-in flag don't crash on upgrade. That shim
    # was never actually implemented, so passing --agui hard-crashed argparse with
    # exit 2 — the exact breakage the promise meant to prevent (gh #38). Accept and
    # ignore it (hidden from --help; the env-var half was already tolerated).
    parser.add_argument("--agui", action="store_true", help=argparse.SUPPRESS)
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
        text = cfg.describe(omit_keys=["host", "port", "debug", "title"])
        # A resolved value with a non-Latin-1 char (a CJK/Cyrillic agent spec or
        # project path) made this raw-text print crash with UnicodeEncodeError on the
        # cp1252 (strict) stdout the extension spawns the sidecar on, emitting nothing.
        # Degrade unrepresentable chars to escapes via the shared cp1252-safe writer
        # instead of crashing (gh #42; shared with --message so they can't drift, #51).
        _write_safe(sys.stdout, text + "\n")
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

    # Hand the RESOLVED workspace to the agent via the shared source of truth
    # (ADR 0005). cfg.workspace_root already applied precedence (CLI --workspace >
    # env > toml), so it is authoritative; apply_workspace publishes it to the env
    # the agent reads (canonical + legacy names) and records it as the active
    # workspace for workspace_root(). Replaces the manual env-assign (gh #19).
    apply_workspace(cfg.workspace_root)
    try:
        graph = load_agent_spec(spec)
    except Exception as exc:  # noqa: BLE001
        return fail(f"failed to load agent {spec!r}: {type(exc).__name__}: {exc}")

    # Operate the agent from the workspace as cwd (ADR 0006), AFTER resolving the
    # spec (a relative -a ./x.py:graph must resolve against the invocation cwd, cf.
    # cli gh #30) — so a bring-your-own agent's raw relative file writes land in the
    # workspace instead of the launch cwd, matching cli. Single-process, single-agent.
    os.chdir(workspace_root())

    # One-shot: drive a single turn and print the reply, then exit — no interactive
    # command loop, no caller-crafted NDJSON + shutdown line (gh #48).
    if args.message is not None:
        return _run_once(graph, args.message, spec=spec, as_json=args.json)

    run(graph, sys.stdin, sys.stdout, spec=spec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
