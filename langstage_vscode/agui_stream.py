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

from typing import Any, AsyncIterator, Callable, Dict

_IMPORT_HINT = 'the AG-UI path needs the agui extra: pip install "langstage-vscode[agui]"'

# Yielded by ``stream_events_sync`` when a cooperative ``cancel`` stopped the turn
# mid-flight (gh #93). A distinct sentinel — not a frame dict — so the sidecar's pump
# tells "the turn was cancelled" apart from "the turn produced a frame"/"the turn ended
# naturally" (StopIteration) without a fragile in-band marker. ``is``-compared.
STREAM_CANCELLED = object()

# How often the cancel-aware pump wakes to check ``should_cancel`` WHILE waiting for the
# next frame. The gh #93 bug was that a turn producing no frames for seconds (a node
# awaiting a slow model/tool) blocked the single-threaded pump inside one
# ``__anext__`` — so a `cancel` was never observed until the turn finished on its own.
# Polling on this cadence bounds cancel latency to ~this regardless of frame cadence.
_CANCEL_POLL_INTERVAL = 0.05


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
    extractors: Any = (),
) -> AsyncIterator[Dict[str, Any]]:
    """Drive ``agent.run()`` in-process and yield ``event_to_dict``-shaped frames.

    content  <- TextMessageContentEvent
    tool_start <- ToolCall{Start,Args,End}
    tool_end   <- ToolCallResultEvent
    extraction <- a wired ``ToolExtractor``'s output (only when ``extractors`` is
                  non-empty — e.g. the ``--demo=tools`` path passes ``demo_extractors()``,
                  gh #77; ``()`` for a plain agent means no ``extraction`` frame)
    interrupt  <- CustomEvent(on_interrupt)
    error      <- RunErrorEvent ; complete at the end.

    ``resume`` (answering an interrupt) rides ``forwarded_props.command.resume``.

    The mapping itself lives in the core (``agui.iter_event_frames``, 0.6.16) —
    shared with the web ``SessionAdapter`` — so a rendering fix lands once.
    """
    from langstage_core.agui import iter_event_frames

    async for frame in iter_event_frames(
        agent, message, thread_id, resume=resume, max_result_len=max_result_len,
        extractors=extractors,
    ):
        yield frame


async def _anext_or_cancel(
    agen: AsyncIterator[Dict[str, Any]],
    should_cancel: Callable[[], bool],
    poll_interval: float,
) -> Any:
    """Await the generator's next frame, but cancel the turn mid-flight if
    ``should_cancel`` fires while we wait (gh #93).

    The next frame is driven as its own task so we can wake on a timer to poll
    ``should_cancel`` INSTEAD of blocking indefinitely inside a single ``__anext__``
    — the exact gap that made 0.5.25's cancel inert: a turn that awaited seconds
    without yielding a frame (a slow model/tool call) never let the loop observe the
    ``cancel`` until the turn had already finished. When cancel fires we
    ``task.cancel()``, which throws ``CancelledError`` into the running generator at
    its current await point (its ``aclose()`` teardown runs and the underlying ag-ui
    run task is torn down), aborting the turn at once — then return the
    ``STREAM_CANCELLED`` sentinel. Otherwise the frame (or a propagated
    ``StopAsyncIteration`` at the turn's natural end) is returned.
    """
    import asyncio

    task = asyncio.ensure_future(agen.__anext__())
    while True:
        done, _ = await asyncio.wait({task}, timeout=poll_interval)
        if task in done:
            return task.result()  # a frame, or raises StopAsyncIteration at turn end
        if should_cancel():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 — CancelledError/StopAsyncIteration/agent error, all discarded on cancel
                pass
            return STREAM_CANCELLED


def stream_events_sync(
    agent, message, thread_id, *, resume=None, max_result_len=50_000, extractors=(),
    should_cancel: Callable[[], bool] | None = None,
    poll_interval: float = _CANCEL_POLL_INTERVAL,
):
    """Sync bridge: pump the async generator. The sidecar's run() loop is a plain
    sync process (no running event loop), so a fresh loop is safe and streaming
    stays lazy (one frame at a time).

    ``should_cancel`` (the raw stdio path, gh #93) is polled WHILE each frame is
    awaited; if it fires the in-flight turn's task is cancelled (aborting the
    underlying ag-ui run at its next await point, running its ``aclose()`` teardown)
    and the ``STREAM_CANCELLED`` sentinel is yielded, then the generator ends. This
    is what makes ``cancel`` abort real work promptly instead of only relabelling the
    turn's natural-completion frame. Left ``None`` (every non-stdio caller) the pump
    is byte-identical to before: a plain blocking ``__anext__`` per frame."""
    import asyncio

    loop = asyncio.new_event_loop()
    agen = agui_events(
        agent, message, thread_id, resume=resume, max_result_len=max_result_len,
        extractors=extractors,
    )
    try:
        while True:
            try:
                if should_cancel is None:
                    yield loop.run_until_complete(agen.__anext__())
                else:
                    frame = loop.run_until_complete(
                        _anext_or_cancel(agen, should_cancel, poll_interval)
                    )
                    yield frame
                    if frame is STREAM_CANCELLED:
                        # The turn was aborted mid-flight; nothing more to pump. The
                        # finally below runs the same teardown the natural-end path does.
                        break
            except StopAsyncIteration:
                break
    finally:
        # Close the async generator (cancelling any still-pending ag-ui run task) and
        # shut down async gens BEFORE closing the loop. Otherwise an exception that
        # escapes mid-stream — or a consumer that stops early — leaves the generator's
        # pending athrow task alive, and asyncio logs "Task was destroyed but it is
        # pending!" to stderr. For a stdio sidecar that stderr is exactly where stray
        # output must not go (gh #40, Defect 2).
        #
        # When the consumer stops the turn MID-stream — a `cancel` command (gh #67) or
        # any early break — the ag-ui run task is still actively driving the graph, so
        # aclose()ing the generator it feeds would try to close an async generator that
        # is "already running" and asyncio prints that failure to stderr. Cancel every
        # still-pending task and let it unwind FIRST, so by the time we aclose there is
        # nothing driving the inner generators and the teardown stays silent.
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        try:
            loop.run_until_complete(agen.aclose())
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            pass
        loop.close()
