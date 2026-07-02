"""Experimental in-process AG-UI streaming path for the vscode sidecar.

ADR 0002 (cli-first pattern, now vscode): drive the agent through the official
``ag-ui-langgraph`` adapter in-process (no web server) and map AG-UI events onto
the SAME ``event_to_dict`` JSON frames the sidecar already emits — so the TS
extension's dispatcher is unchanged.

Unlike the cli/jupyter surfaces (which consume ``stream_graph_updates`` chunk
dicts), the vscode wire is the ``event_to_dict`` vocabulary
(``content``/``tool_start``/``tool_end``/``interrupt``/``complete``/``error``).
That mapping now lives in the core (``agui.iter_event_frames``, 0.6.16) and is
shared with the web ``SessionAdapter``; this module keeps only the thin
session/pump wrappers.

Requires the ``agui`` extra::

    pip install "langstage-vscode[agui]"
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict

_IMPORT_HINT = 'the AG-UI path needs the agui extra: pip install "langstage-vscode[agui]"'


def ensure_agui_available() -> None:
    """Raise a clean, actionable error if the AG-UI adapter isn't installed."""
    try:
        import ag_ui_langgraph  # noqa: F401
        from langstage_core.agui import build_agent  # noqa: F401
    except ImportError as e:  # pragma: no cover - only without the extra
        raise RuntimeError(_IMPORT_HINT) from e


def build_session_agent(graph: Any, *, name: str = "langstage-vscode") -> Any:
    """Wrap the graph once (checkpointer attached by the core bridge); thread_id
    is passed per turn via the session_id, so per-session state persists."""
    ensure_agui_available()
    from langstage_core.agui import build_agent

    return build_agent(graph, name=name)


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

    The mapping itself lives in the core (``agui.iter_event_frames``, 0.6.16) —
    shared with the web ``SessionAdapter`` — so a rendering fix lands once.
    """
    from langstage_core.agui import iter_event_frames

    async for frame in iter_event_frames(
        agent, message, thread_id, resume=resume, max_result_len=max_result_len
    ):
        yield frame


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
