"""stdio sidecar bridging a LangGraph agent to the langstage-vscode extension.

Reads newline-delimited JSON commands on stdin, runs the agent through
``langstage-core``'s in-process AG-UI adapter, and writes newline-delimited JSON
events on stdout. The events are exactly ``event_to_dict()`` shapes — the same
wire vocabulary every other LangStage surface (web, Jupyter, CLI) emits — so the
TS extension's dispatcher renders them the same way.

Commands (client -> sidecar), one JSON object per line:
    {"type": "message",  "session_id": "s1", "content": "..."}
    {"type": "decision", "session_id": "s1", "decisions": [{"type": "approve"}]}
    {"type": "cancel",   "session_id": "s1"}   # abort the in-flight turn, keep the session
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
from collections import deque
from typing import Any, Callable, Iterable, TextIO

from langstage_core import apply_workspace, load_agent_spec, workspace_root

DEFAULT_MAX_RESULT_LEN = 50_000

# Sentinel returned by the command intake when the input stream is exhausted (EOF /
# closed stdin), so an ordinary line and "no more commands" are never confused.
_EOF = object()


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


class _CommandIntake:
    """Feeds ``run()``'s loop one raw command line at a time, in one of two modes.

    DISABLED (every caller except the raw stdio path — ``--repl``/``--message``/
    ``--selfcheck``): a thin inline iterator over ``stdin``. No threads, so the
    REPL's lazy generator (which draws prompt N+1 only after turn N's frames have
    streamed) and the ordered one-shot/selfcheck scripts behave exactly as before.

    ENABLED (the raw stdio path, gh #67): a daemon thread drains ``stdin`` onto a
    queue so a ``cancel`` can be observed WHILE a turn is streaming — the single-
    threaded loop is otherwise blocked pumping frames and never reads stdin mid-turn.
    ``next_command`` still delivers commands in order; ``cancel_check(session_id)``
    returns a between-frames predicate that pulls whatever is currently queued,
    returns ``True`` iff a ``cancel`` for the active session is among it, and stashes
    the rest so they run in order after the turn. A ``cancel`` for a DIFFERENT session
    is stashed (it has no turn to stop here) and errors as "no turn in progress" when
    the loop reaches it — the same guard a cancel with nothing in flight gets.
    """

    def __init__(self, stdin: Iterable[str], *, enabled: bool) -> None:
        self._enabled = enabled
        self._deferred: deque[Any] = deque()
        if not enabled:
            self._it = iter(stdin)
            return
        import queue
        import threading

        self._queue: "queue.Queue[Any]" = queue.Queue()

        def _pump() -> None:
            try:
                for raw in stdin:
                    self._queue.put(raw)
            finally:
                self._queue.put(_EOF)  # unblock the loop's blocking get on EOF

        threading.Thread(target=_pump, daemon=True).start()

    def next_command(self) -> Any:
        """The next raw command line, or ``_EOF`` when the input is exhausted."""
        if self._deferred:
            return self._deferred.popleft()
        if not self._enabled:
            return next(self._it, _EOF)
        return self._queue.get()

    def cancel_check(self, session_id: str) -> Callable[[], bool]:
        """A predicate ``run()`` calls between turn frames: drain the queue, return
        True iff a ``cancel`` for ``session_id`` arrived, and defer everything else so
        the loop processes it in order once the turn ends."""
        import queue

        def _check() -> bool:
            found = False
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _EOF:
                    self._deferred.append(_EOF)
                    break
                if not found and self._is_cancel_for(nxt, session_id):
                    found = True  # consume the cancel; do not defer it
                    continue
                self._deferred.append(nxt)
            return found

        return _check

    @staticmethod
    def _is_cancel_for(raw: Any, session_id: str) -> bool:
        try:
            cmd = json.loads(raw.strip())
        except (json.JSONDecodeError, AttributeError):
            return False
        return (
            isinstance(cmd, dict)
            and cmd.get("type") == "cancel"
            and cmd.get("session_id", "default") == session_id
        )


def run(
    graph: Any,
    stdin: Iterable[str],
    stdout: TextIO,
    *,
    spec: str | None = None,
    max_result_len: int = DEFAULT_MAX_RESULT_LEN,
    enable_cancel: bool = False,
    **_legacy: Any,  # accepts + ignores the removed stream_mode/agui kwargs
) -> None:
    """Drive the command/event loop over the given streams.

    Since langstage-core 1.0 (ADR 0003) turns stream through the in-process AG-UI
    adapter — the only path — emitting ``event_to_dict``-shaped frames so the TS
    extension is unchanged. ``stdin`` is any line iterable; ``stdout`` needs ``write``.

    ``enable_cancel`` (the raw stdio path only) spins up a background reader so a
    ``cancel`` command can stop an in-flight turn cooperatively — the loop is single-
    threaded and, while pumping a turn's frames, never reads stdin (gh #67). It is off
    for every other caller so their synchronous, lazily-pulled input is untouched.
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

    intake = _CommandIntake(stdin, enabled=enable_cancel)

    # Per-session pending-interrupt state, tracked off the frames this loop emits —
    # the raw-protocol analogue of ``_ReplSink.pending_interrupt`` (gh #63). A turn
    # that ends on an ``interrupt`` leaves its session PENDING; the next turn on that
    # session (a resume that completes, or a fresh message) clears it. A ``decision``
    # for a session with nothing pending has no interrupt to resume, so it must error
    # rather than ack + drive a spurious turn (gh #65 — the non-empty sibling of the
    # empty-list case gh #33 already closed). Keyed per session_id/thread because the
    # sidecar can hold several sessions at once.
    pending_interrupts: set[str] = set()

    while True:
        raw = intake.next_command()
        if raw is _EOF:
            break
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
            # ...and a well-formed decision still needs an interrupt to resume: if this
            # session has none pending, there is nothing to resume, so error rather than
            # ack + drive a spurious turn (the invariant the gh #33 comment states, for
            # the non-empty case it left open). (gh #65)
            if session_id not in pending_interrupts:
                emit({"type": "error",
                      "error": f"no interrupt pending for session {session_id!r}"})
                continue
            emit({"type": "ack", "ref": "decision"})
            agui_resume = {"decisions": decisions}
        elif ctype == "cancel":
            # A cancel that reaches the dispatch loop has no turn in flight for this
            # session — a cancel arriving DURING a turn is consumed by that turn's
            # cancel check below and never gets here. Reject it cleanly, like the
            # decision/message guards, rather than acking or crashing. (gh #67)
            emit({"type": "error",
                  "error": f"no turn in progress for session {session_id!r}"})
            continue
        else:
            emit({"type": "error", "error": f"unknown command type: {ctype!r}"})
            continue

        should_cancel = intake.cancel_check(session_id) if enable_cancel else None
        saw_interrupt, cancelled = _run_turn_agui(
            agui_agent, agui_message, agui_resume, config, max_result_len, emit,
            should_cancel=should_cancel,
        )
        # gh #67: a cancelled turn emits `cancelled` (not `complete`) then `turn_end`,
        # and leaves the session/checkpointer intact — the next turn keeps memory. gh
        # #65: otherwise, this turn's interrupt-or-not is the session's new pending
        # state (an interrupt turn leaves it PENDING; anything else clears it).
        if cancelled:
            emit({"type": "cancelled", "session_id": session_id})
            pending_interrupts.discard(session_id)
        elif saw_interrupt:
            pending_interrupts.add(session_id)
        else:
            pending_interrupts.discard(session_id)
        emit({"type": "turn_end", "session_id": session_id})


def _run_turn_agui(
    agent: Any,
    message: str | None,
    resume: Any,
    config: dict[str, Any],
    max_result_len: int,
    emit: Callable[[dict[str, Any]], None],
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[bool, bool]:
    """Stream one turn through the in-process AG-UI adapter, emitting
    ``event_to_dict``-shaped frames (the sidecar's only streaming path, ADR 0003).

    Returns ``(saw_interrupt, cancelled)``: whether the turn emitted an ``interrupt``
    frame (so the session is left paused awaiting a decision, gh #65), and whether it
    was stopped early by a cooperative ``cancel`` (gh #67).

    ``should_cancel`` (raw stdio path only) is checked between frames; when it fires,
    the AG-UI generator is closed — its ``finally: aclose()`` cancels the pending
    ag-ui run task and tears the turn down WITHOUT touching the session/checkpointer,
    so the next turn on this thread still has memory — and the turn returns cancelled.
    """
    from .agui_stream import stream_events_sync

    thread_id = config.get("configurable", {}).get("thread_id", "default")
    saw_interrupt = False
    stream = stream_events_sync(
        agent,
        message or "",
        thread_id,
        resume=resume,
        max_result_len=max_result_len,
    )
    try:
        for frame in stream:
            # gh #67: cooperative cancel. Check before emitting the next frame so a
            # client `cancel` (surfaced here between frames) stops the stream promptly;
            # closing the generator runs its teardown and cancels the ag-ui task.
            if should_cancel is not None and should_cancel():
                stream.close()
                return saw_interrupt, True
            if frame.get("type") == "interrupt":
                saw_interrupt = True
            emit(frame)
    except Exception as exc:  # noqa: BLE001 — surfaced to the client as an event
        emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
    return saw_interrupt, False


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


def _interrupt_actions(frame: dict[str, Any]) -> list[str]:
    """Pull the human-readable action name(s) out of an ``interrupt`` frame's
    ``action_requests``, tolerating both shapes the core normalizer emits.

    A HITL ``interrupt(...)`` reaches the wire as one of two shapes depending on
    the payload the agent passed (verified against langstage-core 1.0.19):

    - a single-dict ``interrupt({...})`` keeps the request NESTED —
      ``{"action_request": {"action": "confirm", ...}, "description": "..."}``;
    - the standard HumanInterrupt LIST ``interrupt([{...}])`` is UNWRAPPED to
      ``{"action": "approve_tool", "args": {...}}`` (gh #40/#44).

    Read ``action`` from the nested ``action_request`` first, else from the top
    level, so the notice names the real action for either shape (a missing/odd
    shape just contributes nothing rather than raising)."""
    actions: list[str] = []
    for req in frame.get("action_requests") or []:
        if not isinstance(req, dict):
            continue
        inner = req.get("action_request")
        if isinstance(inner, dict) and inner.get("action") is not None:
            actions.append(str(inner["action"]))
        elif req.get("action") is not None:
            actions.append(str(req["action"]))
    return actions


# The explicit `--repl` form for answering a pending interrupt, in the same
# `:`-prefixed command namespace as `:quit` (see ``_REPL_QUIT``). Named here because
# the interrupt notice prints it as the literal syntax to type (gh #63).
_DECISION_CMD = ":decision"


def _interrupt_allowed(frame: dict[str, Any]) -> list[str]:
    """The decision verbs THIS interrupt advertises, straight from the frame.

    The single source of truth for what an answer may say: the notice prints it, the
    REPL parser validates against it, and neither hard-codes a verb list — a middleware
    that allows only ``approve``/``reject`` must not have ``edit`` offered or accepted
    (gh #63)."""
    return [str(d) for d in (frame.get("allowed_decisions") or [])]


# Payload grammar per decision verb, for rendering usage hints and parsing a REPL
# answer line. NOTE: which verbs are ACCEPTED never comes from here — that is always
# the frame's own ``allowed_decisions`` (see ``_interrupt_allowed``). This table only
# describes what a given verb's payload looks like, for the four canonical langchain
# HITL decisions (``ApproveDecision``/``RejectDecision``/``RespondDecision``/
# ``EditDecision``). ``(required, kind)``; ``kind=None`` means "takes no payload".
_DECISION_ARGS: dict[str, tuple[bool, str | None]] = {
    "approve": (False, None),
    "reject": (False, "text"),  # optional message explaining the rejection
    "respond": (True, "text"),  # required message answering on the tool's behalf
    "edit": (True, "json"),  # required object, e.g. {"edited_action": {...}}
}
# An advertised verb this build has never heard of: accept it (the frame says it is
# valid) with an optional free-text payload, rather than refusing something the agent
# explicitly allows.
_DECISION_ARGS_DEFAULT: tuple[bool, str | None] = (False, "text")


def _decision_usage(verb: str) -> str:
    """One token of usage help for ``verb`` — ``approve`` / ``reject [<text>]`` /
    ``respond <text>`` / ``edit <json>``."""
    required, kind = _DECISION_ARGS.get(verb, _DECISION_ARGS_DEFAULT)
    if kind is None:
        return verb
    return f"{verb} <{kind}>" if required else f"{verb} [<{kind}>]"


def _format_interrupt_notice(frame: dict[str, Any], *, repl: bool = False) -> str:
    """Render the concise human-mode notice for an ``interrupt`` turn.

    ASCII-only on purpose: this is CLI chrome that must survive a cp1252 (strict)
    console/pipe unmangled (the same constraint the em-dash-free ``--help`` and the
    ``_write_safe`` guard enforce), so no ``U+23F8`` pause glyph. Names the pending
    action(s) and the decisions the frame advertises.

    The closing line depends on WHO can answer (gh #63): one-shot ``--message`` has no
    way to answer (the process exits), so it points at the raw ``decision`` command;
    ``--repl`` CAN answer inline, so it prints the literal syntax to type — built from
    this frame's own advertised verbs, never a hard-coded list, so an interrupt that
    allows only ``approve``/``reject`` never advertises ``edit``."""
    actions = _interrupt_actions(frame)
    allowed = _interrupt_allowed(frame)
    lines = ["interrupt: agent paused awaiting a decision"]
    detail = []
    if actions:
        detail.append("action: " + ", ".join(actions))
    if allowed:
        detail.append("allowed: " + " | ".join(allowed))
    if detail:
        lines.append("  " + "   ".join(detail))
    if not repl:
        lines.append("  resume by sending a `decision` command (add --json to see the full request)")
        return "\n".join(lines) + "\n"
    lines.append(
        f"  answer it here: `{_DECISION_CMD} <verb>` (or a bare `<verb>`) using a verb above"
    )
    payloads = [_decision_usage(v) for v in allowed if _DECISION_ARGS.get(v, _DECISION_ARGS_DEFAULT)[1]]
    if payloads:
        lines.append("  payloads: " + " | ".join(payloads))
    return "\n".join(lines) + "\n"


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
    non-zero on error exit contract ``--message`` has carried since gh #48, and — new
    in gh #58 — whether the turn ended on a HITL ``interrupt`` (no ``content``), which
    used to render as a silent blank exit-0 indistinguishable from an empty reply.
    An interrupt turn now surfaces a concise notice to STDERR (keeping stdout the clean
    reply channel) and flags ``saw_interrupt`` so ``--message`` can exit with a distinct
    non-zero code; ``--json`` still forwards the raw ``interrupt`` frame verbatim (a
    consumer keys on ``type == "interrupt"``). Every write goes through ``_write_safe``,
    so a non-Latin-1 char in the reply still degrades to an escape on a cp1252 stdout
    instead of crashing (gh #51 holds after the switch to streaming).
    """

    # Whether the interrupt notice should advertise the inline `--repl` answer syntax
    # (gh #63). False here: a one-shot `--message` process exits at turn_end, so it can
    # only point at the raw `decision` command. `_ReplSink` flips it.
    _interrupt_answerable = False

    def __init__(self, out: TextIO, err: TextIO, *, as_json: bool) -> None:
        self._out = out
        self._err = err
        self._as_json = as_json
        self._buf = ""
        self.saw_error = False
        self.saw_interrupt = False
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
        elif ftype == "interrupt":
            # A turn that ends on a HITL interrupt (no content) — record it so the
            # one-shot exit code can flag the pause, before the --json early return
            # so it's tracked on both channels (gh #58).
            self.saw_interrupt = True

        if self._as_json:
            # Forward the frame verbatim (run() emitted it via json.dumps, so it's
            # already ASCII-safe), flushing so a piped consumer sees it immediately.
            # The raw `interrupt` frame rides this stream unchanged, so a --json
            # consumer already keys on it (gh #58).
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
        elif ftype == "interrupt":
            # gh #58: never render an interrupt turn as blank. Surface a concise
            # notice on stderr (stdout stays the clean reply channel, exactly like the
            # error path) so a paused agent isn't invisible. The full request is on the
            # --json stream for a consumer that needs the payload.
            _write_safe(
                self._err,
                _format_interrupt_notice(frame, repl=self._interrupt_answerable),
            )
            self._err.flush()

    def close_reply(self) -> None:
        """Terminate a live human-mode reply with the trailing newline the old
        ``print(reply)`` added, so the shell prompt returns on its own line. No-op in
        ``--json`` mode and when the turn produced no ``content``."""
        if self.printed_reply:
            _write_safe(self._out, "\n")
            self._out.flush()


# Lines that end the interactive --repl session. `:quit` is the documented one
# (see the flag help + README); `:q`/`:exit` are conventional REPL aliases accepted
# for muscle memory. A bare EOF (Ctrl-D / closed stdin) ends the session too. These
# are checked BEFORE decision parsing, so `:quit` still works while an interrupt is
# pending — the always-available way out of a paused session (gh #63).
_REPL_QUIT = frozenset({":quit", ":q", ":exit"})


def _parse_repl_decision(
    text: str, allowed: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse one ``--repl`` answer line into a single decision dict.

    Returns ``(decision, None)`` on success or ``(None, reason)`` on refusal — the
    caller surfaces ``reason`` on stderr and re-prompts, so an unparseable answer is
    never silently downgraded to a fresh ``message`` (today's bug, gh #63) nor
    swallowed.

    ``text`` is the answer with any ``:decision`` prefix already stripped: a verb plus
    an optional payload. ``allowed`` is the pending frame's own ``allowed_decisions``
    and is the ONLY source of which verbs are valid — a verb the interrupt didn't
    advertise is refused even if it is a canonical HITL verb. Verb matching is
    case-insensitive but the decision carries the frame's spelling.

    Payload grammar (see ``_DECISION_ARGS``):

    - no payload -> ``{"type": verb}`` (``approve``);
    - a JSON object -> merged into the decision, so the full typed shapes are reachable
      (``edit {"edited_action": {"name": "x", "args": {}}}``); the verb always wins the
      ``type`` key;
    - any other text -> ``{"type": verb, "message": text}`` (``respond``/``reject``,
      whose langchain shapes carry exactly a ``message``).
    """
    verb, _, payload = text.partition(" ")
    payload = payload.strip()
    match = next((a for a in allowed if a.lower() == verb.lower()), None)
    if match is None:
        return None, f"`{verb}` is not a decision this interrupt allows"

    required, kind = _DECISION_ARGS.get(match, _DECISION_ARGS_DEFAULT)
    if kind is None and payload:
        # `approve some junk` is a typo or a misunderstanding, not an approval with a
        # note — refuse rather than send a decision carrying a field the shape has no
        # room for.
        return None, f"`{match}` takes no payload (got {payload!r})"
    if required and not payload:
        return None, f"`{match}` needs a payload -- {_decision_usage(match)}"
    if not payload:
        return {"type": match}, None
    if kind == "json" or payload.startswith("{"):
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            return None, f"`{match}` payload is not valid JSON: {exc}"
        if not isinstance(obj, dict):
            return None, f"`{match}` payload must be a JSON object -- {_decision_usage(match)}"
        return {**obj, "type": match}, None
    return {"type": match, "message": payload}, None


def _decision_refusal_notice(
    text: str, reason: str, allowed: list[str], *, pending: bool
) -> str:
    """The stderr note for a ``--repl`` line that could not be answered as a decision.

    Obvious (names what was typed and why it was refused) and recoverable (says what
    state the session is actually in and what to type next) — the two halves of the
    "never silently misinterpret" contract (gh #63).

    ``pending`` picks which recovery hint is true: with an interrupt still pending, the
    next line gets another try, so repeat the syntax and the verbs THIS frame
    advertises plus the way out; with nothing pending (an explicit ``:decision`` typed
    out of context) there is nothing to answer, so say so and point back at plain chat.

    The chrome is ASCII-only (same cp1252 rule as the interrupt notice); the echoed
    input is whatever the user typed, and the ``_write_safe`` writer guards that."""
    lines = [f"not a decision: {text!r} -- {reason}"]
    if pending:
        hint = (
            "  the interrupt is still pending -- answer with "
            f"`{_DECISION_CMD} <verb>` or a bare `<verb>`"
        )
        if allowed:
            hint += ": " + " | ".join(_decision_usage(v) for v in allowed)
        lines.append(hint)
        lines.append("  (`:quit` ends the session and leaves the interrupt unanswered)")
    else:
        lines.append(
            f"  `{_DECISION_CMD}` answers a pending HITL interrupt; nothing is paused right "
            "now, so send a plain line to chat"
        )
    return "\n".join(lines) + "\n"


class _ReplSink(_OneShotSink):
    """The multi-turn sink for interactive ``--repl``.

    ``--repl`` feeds ``run()`` one ``message`` command per input line and lets the
    SAME ``run()`` loop stay alive across turns — so a checkpointer-backed agent's
    memory persists (gh #54's invariant). This sink reuses ``_OneShotSink``'s live
    ``content`` rendering, ``error``-to-stderr routing, ``--json`` frame forwarding,
    ``saw_error`` tracking, the gh #58 ``interrupt``-notice-to-stderr surfacing, and
    cp1252-safe ``_write_safe`` writes verbatim — the ONLY thing it adds is a per-turn
    boundary: ``run()`` emits ``turn_end`` after every turn, so on each one (in human
    mode) it closes off the streamed reply with the trailing newline ``_OneShotSink``
    would otherwise add just once, and resets for the next turn. In ``--json`` mode
    ``turn_end`` is forwarded verbatim like any other frame (the base class already
    does that), so the raw NDJSON stream is unbroken for a scripting consumer.

    An interrupt turn in a REPL session is surfaced (the stderr notice) but does NOT
    end the interactive session — like a per-turn ``error``, it is visible but
    non-fatal. It also leaves the session in a PENDING state (gh #63): the frame is
    kept in ``pending_interrupt`` so the REPL loop knows the next line must answer it
    (via ``decision``) rather than start a fresh ``message`` turn, and so
    ``_run_repl`` can tell an answered interrupt from one abandoned at exit.

    ``pending_interrupt`` tracks the LIVE session's state off the frames themselves:
    every ``ack`` (a new command was accepted) clears it and an ``interrupt`` sets it,
    so a resumed turn that interrupts AGAIN correctly stays pending, and a resumed turn
    that completes clears. ``run()`` writes frames synchronously into this sink and only
    then pulls the next input line, so by the time the REPL loop reads this attribute it
    already reflects the finished turn.

    The interrupt notice differs from the one-shot one here: ``--repl`` CAN answer, so
    it prints the literal syntax to type instead of pointing at the raw protocol.
    """

    _interrupt_answerable = True  # the notice advertises the inline answer syntax

    def __init__(self, out: TextIO, err: TextIO, *, as_json: bool) -> None:
        super().__init__(out, err, as_json=as_json)
        self.turns = 0  # number of turn_end boundaries seen (a driven turn ran)
        # The interrupt frame this session is currently paused on, or None. See above.
        self.pending_interrupt: dict[str, Any] | None = None

    def _on_frame(self, line: str) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            return
        ftype = frame.get("type")
        if ftype == "ack":
            # A command was accepted: whatever this session was paused on is being
            # acted on now (a `decision` answers it; a `message` starts a fresh turn).
            self.pending_interrupt = None
        elif ftype == "interrupt":
            self.pending_interrupt = frame
        if ftype == "turn_end":
            self.turns += 1
            if not self._as_json:
                # Close the finished reply (trailing newline iff it printed anything)
                # and reset so the next turn streams onto its own line.
                self.close_reply()
                self.printed_reply = False
                return
        # Everything else — content, error, ready/ack/complete, and turn_end in
        # --json mode — is handled exactly as the one-shot sink handles it.
        super()._on_frame(line)


def _run_repl(
    graph: Any,
    *,
    spec: str | None,
    as_json: bool,
    stdin: Iterable[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    session_id: str = "repl",
    prompt: str = "> ",
    show_prompt: bool | None = None,
) -> int:
    """Interactive multi-turn REPL: the conversational companion to one-shot
    ``--message`` (gh #56).

    Reads one prompt per input line, drives a turn, prints the assembled reply, and
    loops until EOF (Ctrl-D) or a ``:quit`` line. Because every turn goes through a
    single long-lived ``run()`` loop under one fixed ``session_id`` (hence one
    ``thread_id``), a checkpointer-backed agent REMEMBERS prior turns — the exact
    per-conversation shape the VS Code extension uses (gh #54), so it reproduces the
    real memory semantics instead of the fresh-process-per-turn shape that hides
    them. This is what makes the README's "does turn 2 remember turn 1?" caveat
    verifiable from the CLI in ten seconds ("tell it your name, ask on the next
    line").

    It is a thin front-end over the existing machinery: each input line becomes a
    ``{"type": "message", ...}`` command and EOF/``:quit`` becomes ``shutdown``, fed
    lazily to ``run()``; the reply is rendered by ``_ReplSink`` (a ``_OneShotSink``
    that also closes each turn's reply at ``turn_end``). Nothing about the turn,
    streaming, or ``_write_safe`` plumbing is duplicated. ``--json`` composes with it
    (raw ``event_to_dict`` frames instead of assembled text), same as ``--message``.

    **Answering a HITL interrupt inline (gh #63).** When a turn ends on an
    ``interrupt``, the session is PENDING (``_ReplSink.pending_interrupt``) and the loop
    switches to DECISION MODE for the next line: it becomes a
    ``{"type": "decision", "session_id": ..., "decisions": [...]}`` command on the SAME
    session (so it lands on that thread's pending interrupt) instead of a fresh
    ``message``. This completes the ``interrupt`` -> ``decision`` round-trip from the
    CLI, which previously required hand-writing NDJSON over the raw protocol. The mode
    is explicit both ways:

    - ``:decision <verb> [payload]`` — the explicit form, in the same ``:``-prefixed
      namespace as ``:quit``, and the form the interrupt notice tells you to type. Used
      outside decision mode it is refused ("no interrupt is pending"), never sent as
      chat text.
    - a BARE ``<verb>`` — accepted only while pending, because that is the only state
      in which it is unambiguous. Outside decision mode a bare ``approve`` is ordinary
      chat text and still starts a normal turn, exactly as before.

    While pending, EVERY line is an answer attempt (``:quit`` excepted): one that
    doesn't parse into a verb the frame advertises is REFUSED on stderr and re-prompted,
    with the interrupt left pending. It is deliberately never downgraded to a fresh
    ``message`` — that silent downgrade is the gh #63 bug (the message just re-interrupts,
    so the answer looks accepted and the round-trip never completes).

    Exit codes: ``1`` if the agent could not even start a turn (e.g. a non-runnable
    spec, where ``run()`` emits an ``error`` and returns before any ``turn_end``); ``2``
    if the session ended (EOF/``:quit``) with an interrupt still PENDING — the same
    "paused awaiting a decision" signal one-shot ``--message`` has carried since gh #58,
    now that ``--repl`` is able to answer and so leaving one unanswered is a real
    outcome; otherwise ``0``, including a turn that interrupted and WAS answered, and a
    session that merely had a bad turn (per-turn ``error`` frames are surfaced to stderr
    but don't fail an interactive session).
    """
    import sys

    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if show_prompt is None:
        # Only draw a prompt for a human at a real TTY, and never in --json mode
        # (machine-readable output). Scripted/piped stdin gets a clean channel.
        isatty = getattr(stdin, "isatty", None)
        show_prompt = (not as_json) and bool(isatty and isatty())

    lines = iter(stdin)
    sink = _ReplSink(stdout, stderr, as_json=as_json)

    def refuse(text: str, reason: str, frame: dict[str, Any] | None) -> None:
        """Surface a refused answer line on stderr (stdout stays the reply/NDJSON
        channel, exactly like the error and interrupt notices) and leave any pending
        interrupt pending, so the next line gets another try."""
        _write_safe(
            stderr,
            _decision_refusal_notice(
                text,
                reason,
                _interrupt_allowed(frame or {}),
                pending=frame is not None,
            ),
        )
        stderr.flush()

    def commands() -> Iterable[str]:
        """Translate REPL input lines into NDJSON commands for run(), lazily — so
        the prompt for turn N+1 is drawn only after turn N's reply has streamed.

        Laziness is also what makes decision mode correct: ``run()`` pulls the next
        line only after the previous turn's frames have gone through the sink, so
        ``sink.pending_interrupt`` here already reflects the finished turn."""
        while True:
            # Read the live session's state fresh each iteration: an interrupt from the
            # turn that just finished puts this line in decision mode (gh #63).
            pending = sink.pending_interrupt
            if show_prompt:
                # A distinct prompt makes the mode switch visible at a TTY, on top of
                # the notice the interrupt already printed.
                _write_safe(stderr, ("decision" + prompt) if pending else prompt)
                stderr.flush()
            line = next(lines, None)
            if line is None:  # EOF (Ctrl-D / closed stdin): end the session.
                if show_prompt:
                    _write_safe(stderr, "\n")  # move off the dangling prompt line
                    stderr.flush()
                yield json.dumps({"type": "shutdown"})
                return
            text = line.strip()
            if not text:
                continue  # blank line: re-prompt, don't drive an empty-content turn
            # Checked first, so `:quit` is always available — including as the way out
            # of a pending interrupt you don't want to answer.
            if text in _REPL_QUIT:
                yield json.dumps({"type": "shutdown"})
                return

            explicit = text == _DECISION_CMD or text.startswith(_DECISION_CMD + " ")
            if explicit or pending is not None:
                # Decision mode. Either the user asked for it explicitly with
                # `:decision ...`, or an interrupt is pending and EVERY line is an
                # answer attempt. A line that doesn't parse is refused out loud and
                # re-prompted — never silently downgraded to a `message` (which would
                # just re-interrupt and look like it worked, the gh #63 bug) and never
                # swallowed.
                answer = text[len(_DECISION_CMD):].strip() if explicit else text
                if pending is None:
                    refuse(text, "no interrupt is pending", None)
                    continue
                if not answer:
                    refuse(text, f"`{_DECISION_CMD}` needs a verb", pending)
                    continue
                decision, reason = _parse_repl_decision(
                    answer, _interrupt_allowed(pending)
                )
                if decision is None:
                    refuse(text, reason or "unparseable", pending)
                    continue
                # Same session_id (hence thread_id) as the turn that interrupted, so
                # the decision resumes THAT pending interrupt (gh #63).
                yield json.dumps(
                    {
                        "type": "decision",
                        "session_id": session_id,
                        "decisions": [decision],
                    }
                )
                continue

            yield json.dumps(
                {"type": "message", "session_id": session_id, "content": text}
            )

    run(graph, commands(), sink, spec=spec)

    # A pre-loop failure (e.g. a non-runnable spec) emits an error before any turn
    # ran, so no turn_end was seen — treat that as a hard start failure (exit 1),
    # mirroring --message.
    if sink.saw_error and sink.turns == 0:
        return 1
    # The session ended while the agent was still paused awaiting a decision: the same
    # "unanswered interrupt" signal --message has exited 2 on since gh #58. Before gh #63
    # --repl could not answer, so an interrupt was purely informational and this was 0;
    # now that answering is a first-class REPL action, walking away from one is a real,
    # scriptable outcome (`printf 'do it\napprove\n' | ... --repl; echo $?` -> 0 proves
    # the round-trip completed; 2 proves it did not).
    if sink.pending_interrupt is not None:
        return 2
    # Clean termination — including an interrupt that WAS answered and resumed, and a
    # session that merely had a bad turn.
    return 0


def _run_once(graph: Any, message: str, *, spec: str | None, as_json: bool) -> int:
    """Drive exactly ONE turn with ``message`` and stream the result, then exit — no
    ``shutdown`` handshake required from the caller (gh #48).

    Human mode types out the assistant reply (the ``content`` frames) as it's
    produced; ``--json`` streams the raw ``event_to_dict`` frames (one per line) for
    scripting. Both stream frame-by-frame as ``run()`` emits them rather than
    buffering the whole turn and dumping it at the end (gh #50) — so a slow or
    token-streaming agent no longer looks frozen.

    Exit code is a three-way signal (gh #58): ``0`` on a clean reply, ``1`` if an
    ``error`` frame appears (the stdio loop's ``error`` contract), and ``2`` if the
    turn ended on a HITL ``interrupt`` (the agent paused awaiting a decision) — a
    distinct, scriptable code so an interrupt turn is no longer a silent blank exit-0
    indistinguishable from an empty reply. The notice is on stderr in human mode and
    the raw ``interrupt`` frame is on the ``--json`` stream; the exit code flags it on
    either. Internally this is the ``run()`` loop fed an in-memory
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

    if sink.saw_error:
        return 1
    if sink.saw_interrupt:
        return 2  # the turn paused on an interrupt awaiting a decision (gh #58)
    return 0


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
        "(0 on a reply, 2 if the agent pauses on a HITL interrupt, 1 on an error frame) "
        "with no stdio protocol handshake. Pair with --json to emit the raw event frames "
        "instead of the assembled text.",
    )
    parser.add_argument(
        "--repl",
        action="store_true",
        help="Interactive multi-turn REPL: read one prompt per line, drive a turn, print the "
        "reply, and loop over ONE long-lived session (same session_id) so a checkpointer-backed "
        "agent remembers prior turns. The multi-turn companion to --message; verifies "
        "conversational memory from the CLI. If a turn pauses on a HITL interrupt, answer it "
        "inline with ':decision <verb>' (or a bare verb) to resume the same session. "
        "Exit with EOF (Ctrl-D) or a ':quit' line (0 clean, 2 if an interrupt was left "
        "unanswered). Pair with --json for raw event frames.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --selfcheck, emit a machine-readable JSON verdict; with --message or --repl, "
        "emit the raw event_to_dict frames (one per line) instead of the assembled reply text.",
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

    # --message is one-shot, --repl is multi-turn interactive; both drive turns but
    # over different input models, so asking for both is a contradiction.
    if args.repl and args.message is not None:
        return fail("--repl and --message are mutually exclusive")

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

    # Interactive multi-turn: read a prompt per line and drive turns over ONE
    # long-lived session so a checkpointer-backed agent remembers prior turns —
    # the conversational companion to --message (gh #56).
    if args.repl:
        return _run_repl(graph, spec=spec, as_json=args.json)

    # The raw stdio command loop (what the VS Code extension drives). enable_cancel
    # spins up the background reader so a `cancel` command can stop an in-flight turn
    # cooperatively — keeping the long-lived session and its in-process checkpointer —
    # instead of the extension having to kill the whole sidecar (gh #67).
    run(graph, sys.stdin, sys.stdout, spec=spec, enable_cancel=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
