"""A keyless test agent for the panel's end-to-end tests (test/e2e/).

The built-in ``--demo=tools`` agent covers streaming, tools, reasoning and an
interrupt, but its interrupt allows only ``respond`` and ``approve``, and its turns are
too quick to stop reliably. This graph fills those two gaps, with no model and no key:

- ``"write a file"``: an interrupt shaped like HumanInTheLoopMiddleware's, allowing all
  four verbs (approve / edit / reject / respond) on a ``write_file`` action; the resumed
  reply echoes the decision it received;
- ``"count slowly"``: streams numbered tokens half a second apart (30 s in all), so Stop lands
  mid-turn;
- anything else: echoes the message.

Run: ``python -m langstage_vscode --agent extension/test/fixtures/hitl_agent.py:graph``.
Test-only; not shipped in the VSIX.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

WRITE_REQUEST: dict[str, Any] = {
    "action_requests": [
        {
            "name": "write_file",
            "args": {"path": "notes.txt", "content": "hello from the agent"},
            "description": "Write notes.txt in the workspace",
        }
    ],
    "allowed_decisions": ["approve", "edit", "reject", "respond"],
}


def _last_human(messages: List[BaseMessage]) -> str:
    for m in reversed(messages):
        if getattr(m, "type", None) == "human":
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


class SlowCounter(BaseChatModel):
    """Streams "1 2 3 …" a token at a time, slowly."""

    @property
    def _llm_type(self) -> str:
        return "slow-counter"

    def _text(self) -> list[str]:
        return [f"{i} " for i in range(1, 61)]

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="".join(self._text())))])

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for piece in self._text():
            time.sleep(0.5)
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager is not None:
                run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk


_counter = SlowCounter()


def _agent(state: MessagesState) -> dict:
    text = _last_human(state["messages"]).lower()
    if "write a file" in text:
        decision = interrupt(WRITE_REQUEST)
        decisions = decision.get("decisions") if isinstance(decision, dict) else decision
        return {"messages": [AIMessage(content=f"Resumed with {json.dumps(decisions, sort_keys=True)}")]}
    if "count slowly" in text:
        return {"messages": [_counter.invoke(state["messages"])]}
    return {"messages": [AIMessage(content=f"(hitl fixture) You said: {_last_human(state['messages'])}")]}


_builder = StateGraph(MessagesState)
_builder.add_node("agent", _agent)
_builder.add_edge(START, "agent")
_builder.add_edge("agent", END)
graph = _builder.compile()
