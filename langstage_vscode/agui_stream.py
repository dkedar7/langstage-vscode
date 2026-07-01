"""Experimental in-process AG-UI streaming path for the vscode sidecar.

ADR 0002 (cli-first pattern, now vscode): drive the agent through the official
``ag-ui-langgraph`` adapter in-process (no web server) and map AG-UI events onto
the SAME ``event_to_dict`` JSON frames the sidecar already emits — so the TS
extension's dispatcher is unchanged.

Unlike the cli/jupyter surfaces (which consume ``stream_graph_updates`` chunk
dicts), the vscode wire is the ``event_to_dict`` vocabulary
(``content``/``tool_start``/``tool_end``/``interrupt``/``complete``/``error``),
so this mapping is vscode-specific.

Requires the ``agui`` extra::

    pip install "langstage-vscode[agui]"
"""
from __future__ import annotations

import json
import uuid
from typing import Any, AsyncIterator, Dict

_IMPORT_HINT = 'the AG-UI path needs the agui extra: pip install "langstage-vscode[agui]"'
_ALLOWED_DECISIONS = ["reject", "edit", "respond", "approve"]


def ensure_agui_available() -> None:
    """Raise a clean, actionable error if the AG-UI adapter isn't installed."""
    try:
        import ag_ui_langgraph  # noqa: F401
        from langgraph_stream_parser.agui import build_agent  # noqa: F401
    except ImportError as e:  # pragma: no cover - only without the extra
        raise RuntimeError(_IMPORT_HINT) from e


def build_session_agent(graph: Any, *, name: str = "langstage-vscode") -> Any:
    """Wrap the graph once (checkpointer attached by the core bridge); thread_id
    is passed per turn via the session_id, so per-session state persists."""
    ensure_agui_available()
    from langgraph_stream_parser.agui import build_agent

    return build_agent(graph, name=name)


def _truncate(text: str, max_result_len: int) -> str:
    return text if len(text) <= max_result_len else text[:max_result_len] + "…(truncated)"


async def agui_events(
    agent: Any,
    message: str,
    thread_id: str,
    *,
    resume: Any = None,
    max_result_len: int = 50_000,
) -> AsyncIterator[Dict[str, Any]]:
    """Drive ``agent.run()`` in-process and yield ``event_to_dict``-shaped frames.

    content  <- TextMessageContentEvent
    tool_start <- ToolCall{Start,Args,End}
    tool_end   <- ToolCallResultEvent
    interrupt  <- CustomEvent(on_interrupt)
    error      <- RunErrorEvent ; complete at the end.

    ``resume`` (answering an interrupt) rides ``forwarded_props.command.resume``.
    """
    from ag_ui.core.types import RunAgentInput, UserMessage

    forwarded_props: Dict[str, Any] = {}
    if resume is not None:
        forwarded_props = {"command": {"resume": resume}}

    run_input = RunAgentInput(
        thread_id=thread_id,
        run_id=str(uuid.uuid4()),
        state={},
        messages=[UserMessage(id=str(uuid.uuid4()), role="user", content=message)],
        tools=[],
        context=[],
        forwarded_props=forwarded_props,
    )

    streamed_text = False
    tool_args: Dict[str, str] = {}
    tool_names: Dict[str, str] = {}

    async for ev in agent.run(run_input):
        t = type(ev).__name__
        if t == "TextMessageContentEvent":
            streamed_text = True
            yield {"type": "content", "content": ev.delta, "role": "assistant", "node": "agent"}
        elif t == "ToolCallStartEvent":
            tool_names[ev.tool_call_id] = ev.tool_call_name
            tool_args[ev.tool_call_id] = ""
        elif t == "ToolCallArgsEvent":
            tool_args[ev.tool_call_id] = tool_args.get(ev.tool_call_id, "") + ev.delta
        elif t == "ToolCallEndEvent":
            raw = tool_args.pop(ev.tool_call_id, "")
            try:
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = {"_raw": raw}
            yield {
                "type": "tool_start",
                "id": ev.tool_call_id,
                "name": tool_names.get(ev.tool_call_id, "tool"),
                "args": args,
                "node": "agent",
            }
        elif t == "ToolCallResultEvent":
            yield {
                "type": "tool_end",
                "id": ev.tool_call_id,
                "name": tool_names.get(ev.tool_call_id, "tool"),
                "result": _truncate(str(getattr(ev, "content", "")), max_result_len),
                "status": "success",
                "error_message": None,
                "duration_ms": None,
            }
        elif t == "CustomEvent" and getattr(ev, "name", None) == "on_interrupt":
            payload = getattr(ev, "value", None)
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            payload = payload or {}
            yield {
                "type": "interrupt",
                "action_requests": payload.get("action_requests", []),
                "review_configs": payload.get("review_configs", []),
                "allowed_decisions": payload.get("allowed_decisions", _ALLOWED_DECISIONS),
            }
        elif t == "MessagesSnapshotEvent" and not streamed_text:
            for m in ev.messages:
                if getattr(m, "role", None) == "assistant" and getattr(m, "content", None):
                    yield {
                        "type": "content",
                        "content": m.content,
                        "role": "assistant",
                        "node": "agent",
                    }
        elif t == "RunErrorEvent":
            yield {"type": "error", "error": getattr(ev, "message", "unknown error")}
            return  # error is terminal; no trailing complete

    yield {"type": "complete"}


def stream_events_sync(agent, message, thread_id, *, resume=None, max_result_len=50_000):
    """Sync bridge: pump the async generator. The sidecar's run() loop is a plain
    sync process (no running event loop), so a fresh loop is safe and streaming
    stays lazy (one frame at a time)."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        agen = agui_events(
            agent, message, thread_id, resume=resume, max_result_len=max_result_len
        )
        while True:
            try:
                yield loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
    finally:
        loop.close()
