"""Tests for the in-process AG-UI sidecar path — the sidecar's only streaming
path since core 1.0 (ADR 0003).

Guarded by importorskip as a safety net, but base deps pull the AG-UI runtime
(core's [agui] extra) so CI always runs these. The path drives a real LangGraph
agent, so these use real compiled graphs.
"""
import io
import json
import time
from typing import Iterator, List

import pytest

pytest.importorskip("ag_ui_langgraph")
pytest.importorskip("fastapi")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.types import interrupt  # noqa: E402
from langstage_core import load_agent_spec  # noqa: E402

from langstage_vscode.sidecar import run  # noqa: E402


def drive(graph, commands):
    stdin = io.StringIO("".join(json.dumps(c) + "\n" for c in commands) + json.dumps({"type": "shutdown"}) + "\n")
    stdout = io.StringIO()
    run(graph, stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def test_text_frames_match_the_wire_shape():
    graph = load_agent_spec("langstage_core.demo.stub:graph")
    frames = drive(graph, [{"type": "message", "session_id": "s", "content": "hi there"}])
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "ready" and "ack" in kinds and kinds[-1] == "turn_end"
    content = [f for f in frames if f["type"] == "content"]
    assert content and all(set(f) == {"type", "content", "role", "node"} for f in content)
    assert "hi there" in "".join(f["content"] for f in content)
    assert any(f["type"] == "complete" for f in frames)


@tool
def get_weather(city: str) -> str:
    """Get the weather."""
    return "Sunny, 72F"


class _FakeToolModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _stream(self, messages: List[BaseMessage], stop=None, run_manager=None, **kwargs) -> Iterator[ChatGenerationChunk]:
        if any(isinstance(m, ToolMessage) for m in messages):
            yield ChatGenerationChunk(message=AIMessageChunk(content="Sunny."))
        else:
            yield ChatGenerationChunk(message=AIMessageChunk(
                content="", tool_call_chunks=[{"name": "get_weather", "args": "", "id": "c1", "index": 0}]))
            for seg in ('{"city": ', '"PDX"}'):
                yield ChatGenerationChunk(message=AIMessageChunk(
                    content="", tool_call_chunks=[{"name": None, "args": seg, "id": None, "index": 0}]))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        chunks = list(self._stream(messages))
        msg = chunks[0].message
        for c in chunks[1:]:
            msg = msg + c.message
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content=msg.content, tool_calls=getattr(msg, "tool_calls", [])))])


def test_tool_start_and_end_frames():
    from langgraph.prebuilt import create_react_agent

    frames = drive(create_react_agent(_FakeToolModel(), [get_weather]),
                   [{"type": "message", "session_id": "s", "content": "weather?"}])
    starts = [f for f in frames if f["type"] == "tool_start"]
    ends = [f for f in frames if f["type"] == "tool_end"]
    assert starts and starts[0]["name"] == "get_weather" and starts[0]["args"] == {"city": "PDX"}
    assert ends and ends[0]["name"] == "get_weather" and ends[0]["result"] == "Sunny, 72F"
    assert set(ends[0]) == {"type", "id", "name", "result", "status", "error_message", "duration_ms"}


def _interrupt_graph():
    # The STANDARD HumanInterrupt shape a real deepagents/langchain HITL agent emits —
    # its action_request is {"action": <tool>, "args": {...}} (key `action`, NOT `tool`).
    # The old test used a fictional {"tool": ...} shape that masked gh #44 (the extension
    # read `.tool`, which the runtime never emits).
    def gate(state):
        d = interrupt([{"action_request": {"action": "approve_tool", "args": {"x": 1}},
                        "config": {"allow_accept": True}}])
        return {"messages": [AIMessage(content=f"ok {d}")]}

    b = StateGraph(MessagesState)
    b.add_node("gate", gate)
    b.add_edge(START, "gate")
    b.add_edge("gate", END)
    return b.compile(checkpointer=InMemorySaver())


def test_interrupt_frame_then_resume_continues():
    frames = drive(_interrupt_graph(), [
        {"type": "message", "session_id": "s", "content": "go"},
        {"type": "decision", "session_id": "s", "decisions": [{"type": "accept"}]},
    ])
    interrupts = [f for f in frames if f["type"] == "interrupt"]
    # The frame carries the real action under `action` (what the extension renders, gh #44).
    assert interrupts and interrupts[0]["action_requests"][0]["action"] == "approve_tool"
    assert "tool" not in interrupts[0]["action_requests"][0]  # not the fictional shape
    assert "allowed_decisions" in interrupts[0]
    # after the decision, the graph continues and emits the resolved content
    text = "".join(f["content"] for f in frames if f["type"] == "content")
    assert "ok" in text and "accept" in text


def _human_interrupt_list_graph():
    """The STANDARD HumanInterrupt list shape deepagents / langchain HITL emit —
    the shape that used to crash the turn (gh #40)."""
    def gate(state):
        req = [{
            "action_request": {"action": "delete_file", "args": {"path": "/tmp/x"}},
            "config": {"allow_accept": True, "allow_respond": True},
            "description": "Approve deleting the file?",
        }]
        d = interrupt(req)
        return {"messages": [AIMessage(content=f"ok {d}")]}

    b = StateGraph(MessagesState)
    b.add_node("gate", gate)
    b.add_edge(START, "gate")
    b.add_edge("gate", END)
    return b.compile(checkpointer=InMemorySaver())


def test_standard_human_interrupt_list_surfaces_instead_of_crashing():
    # gh #40: driving the sidecar with the standard HumanInterrupt LIST used to yield
    # {"type":"error","error":"'list' object has no attribute 'get'"}. It must now
    # surface a real interrupt frame with the action_request populated, then resume.
    frames = drive(_human_interrupt_list_graph(), [
        {"type": "message", "session_id": "s", "content": "delete it"},
        {"type": "decision", "session_id": "s", "decisions": [{"type": "accept"}]},
    ])
    assert not any(f["type"] == "error" for f in frames), frames
    interrupts = [f for f in frames if f["type"] == "interrupt"]
    assert interrupts, [f["type"] for f in frames]
    # the HumanInterrupt's action_request is unwrapped into action_requests
    assert interrupts[0]["action_requests"] == [
        {"action": "delete_file", "args": {"path": "/tmp/x"}}
    ]
    # config allow_accept + allow_respond derived the allowed decisions
    assert set(interrupts[0]["allowed_decisions"]) == {"approve", "respond"}
    # and the round-trip resumes after the decision
    text = "".join(f["content"] for f in frames if f["type"] == "content")
    assert "ok" in text


# ── gh #67: cooperative per-turn cancel over the raw stdio path ──────────────


class _CountingStreamModel(BaseChatModel):
    """Streams content chunks. On the human input ``"LONG"`` it streams many chunks
    slowly (so a mid-stream `cancel` reliably lands between frames); on anything else it
    streams ``count=<n>`` where n is how many messages it was handed — proof that the
    thread's checkpointer survived a prior cancel."""

    @property
    def _llm_type(self) -> str:
        return "counting-stream"

    def bind_tools(self, tools, **kwargs):
        return self

    def _stream(self, messages: List[BaseMessage], stop=None, run_manager=None, **kwargs) -> Iterator[ChatGenerationChunk]:
        from langchain_core.messages import HumanMessage

        last_human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        text = last_human.content if last_human else ""
        if text == "LONG":
            for _ in range(2000):
                time.sleep(0.002)
                yield ChatGenerationChunk(message=AIMessageChunk(content="x"))
        else:
            yield ChatGenerationChunk(message=AIMessageChunk(content=f"count={len(messages)}"))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        chunks = list(self._stream(messages))
        msg = chunks[0].message
        for c in chunks[1:]:
            msg = msg + c.message
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=msg.content))])


def test_cancel_stops_a_streaming_turn_mid_stream_and_keeps_the_session():
    # gh #67 end-to-end over the real raw stdio path (enable_cancel=True): a `cancel`
    # stops an in-flight streaming turn cooperatively — a distinct `cancelled` frame,
    # then `turn_end`, with NO `complete` — WITHOUT tearing down the process, so the
    # long-lived session + its in-process checkpointer survive and the next turn keeps
    # memory. The stdin is gated so the cancel targets the LONG turn (turn 2), exactly
    # like a well-behaved client that waits for turn_end before sending the next line.
    import threading

    from langgraph.prebuilt import create_react_agent

    graph = create_react_agent(_CountingStreamModel(), [], checkpointer=InMemorySaver())

    warm_done = threading.Event()   # turn 1 (warmup) finished -> its memory is committed
    streaming = threading.Event()   # turn 2 (LONG) has started streaming content

    class _GatedStdin:
        def __iter__(self):
            yield json.dumps({"type": "message", "session_id": "s", "content": "warmup"})
            warm_done.wait(15)
            yield json.dumps({"type": "message", "session_id": "s", "content": "LONG"})
            streaming.wait(15)
            yield json.dumps({"type": "cancel", "session_id": "s"})
            yield json.dumps({"type": "message", "session_id": "s", "content": "again"})
            yield json.dumps({"type": "shutdown"})

    frames: List[dict] = []

    class _WatchStdout:
        def write(self, s: str) -> None:
            for line in s.splitlines():
                line = line.strip()
                if not line:
                    continue
                f = json.loads(line)
                frames.append(f)
                t = f.get("type")
                if t == "turn_end" and not warm_done.is_set():
                    warm_done.set()
                elif t == "content" and warm_done.is_set() and not streaming.is_set():
                    streaming.set()

        def flush(self) -> None:
            pass

    run(graph, _GatedStdin(), _WatchStdout(), enable_cancel=True)

    types = [f["type"] for f in frames]
    assert "error" not in types, [f for f in frames if f.get("type") == "error"]
    # A distinct `cancelled` terminal frame, immediately followed by turn_end.
    assert "cancelled" in types, types
    ci = types.index("cancelled")
    assert frames[ci]["session_id"] == "s"
    assert types[ci + 1] == "turn_end"
    # The cancelled (LONG) turn produced no `complete` — cancelled is neither complete
    # nor error. Its frames sit between the 2nd `ack` and the `cancelled`.
    second_ack = [i for i, t in enumerate(types) if t == "ack"][1]
    assert "complete" not in types[second_ack:ci]

    # The session survived: turn 3 ran on the SAME session and remembered turn 1.
    counts = [f["content"] for f in frames
              if f["type"] == "content" and str(f.get("content", "")).startswith("count=")]
    assert counts, frames
    assert counts[0] == "count=1"                 # warmup saw only its own human message
    assert int(counts[-1].split("=")[1]) > 1      # turn 3 remembered -> checkpointer survived


def test_no_task_destroyed_warning_leaks_to_stderr(tmp_path):
    # gh #40, Defect 2: an interrupt turn used to leak asyncio's
    # "Task was destroyed but it is pending!" to the sidecar's stderr (stream_events_sync
    # closed the loop without draining the async generator). A stdio sidecar must keep
    # stderr clean. Driven as a real subprocess — the warning only fires on loop teardown.
    import subprocess
    import sys
    import textwrap

    agent = tmp_path / "hitl.py"
    agent.write_text(textwrap.dedent('''
        from langgraph.graph import StateGraph, START, END, MessagesState
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.types import interrupt
        from langchain_core.messages import AIMessage
        def ask(state):
            interrupt([{"action_request": {"action": "rm", "args": {"p": "/x"}},
                        "config": {"allow_accept": True}, "description": "ok?"}])
            return {"messages": [AIMessage(content="done")]}
        g = StateGraph(MessagesState); g.add_node("ask", ask)
        g.add_edge(START, "ask"); g.add_edge("ask", END)
        graph = g.compile(checkpointer=MemorySaver())
    '''), encoding="utf-8")
    cmds = (json.dumps({"type": "message", "session_id": "s1", "content": "go"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n")
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--agent", f"{agent}:graph"],
        input=cmds, capture_output=True, text=True, timeout=60,
    )
    assert "Task was destroyed" not in proc.stderr, proc.stderr
    # sanity: the interrupt actually surfaced (not an error) on stdout
    assert '"type": "interrupt"' in proc.stdout
    assert "has no attribute 'get'" not in proc.stdout
