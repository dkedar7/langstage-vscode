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
    def gate(state):
        d = interrupt({"action_requests": [{"tool": "approve", "args": {"x": 1}}]})
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
    assert interrupts and interrupts[0]["action_requests"][0]["tool"] == "approve"
    assert "allowed_decisions" in interrupts[0]
    # after the decision, the graph continues and emits the resolved content
    text = "".join(f["content"] for f in frames if f["type"] == "content")
    assert "ok" in text and "accept" in text
