"""Tests for the in-process AG-UI sidecar path — the sidecar's only streaming
path since core 1.0 (ADR 0003).

Guarded by importorskip as a safety net, but base deps pull the AG-UI runtime
(core's [agui] extra) so CI always runs these. The path drives a real LangGraph
agent, so these use real compiled graphs.
"""
import io
import json
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
