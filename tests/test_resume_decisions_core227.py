"""A decision HumanInTheLoopMiddleware would refuse leaves the approval answerable (core #227).

The sidecar already refuses a verb the pending interrupt doesn't allow (gh #117). A
decision with an allowed verb but the wrong shape (an ``edit`` with no
``edited_action``, a ``respond`` with no ``message``) used to be ACKed and sent; the
middleware refused it, the turn errored, and the session lost its pending interrupt, so
a correct answer was then refused with "no interrupt pending". It is now checked with
``langstage_core.decision_problems`` before the ACK and refused like a bad verb:
``error -> turn_end``, nothing sent, and the approval stays pending.
"""
import io
import json

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from langstage_vscode.sidecar import run


@tool
def send_email(to: str) -> str:
    """Send an email."""
    return f"sent to {to}"


class _Model(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):
        if any(isinstance(m, ToolMessage) for m in messages):
            msg = AIMessage(content="Done.")
        else:
            msg = AIMessage(content="", tool_calls=[
                {"name": "send_email", "args": {"to": "a@example.org"}, "id": "c1"}])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _graph():
    return create_agent(_Model(), tools=[send_email], middleware=[
        HumanInTheLoopMiddleware(interrupt_on={"send_email": {"allowed_decisions": ["approve", "edit"]}}),
    ])


def _turns(commands):
    stdin = io.StringIO("".join(json.dumps(c) + "\n" for c in commands))
    stdout = io.StringIO()
    run(_graph(), stdin, stdout)
    turns, cur = [], []
    for ln in stdout.getvalue().splitlines():
        f = json.loads(ln)
        if f["type"] == "ready":
            continue
        cur.append(f)
        if f["type"] == "turn_end":
            turns.append(cur)
            cur = []
    return turns


def test_malformed_edit_is_refused_and_approve_still_answers():
    turns = _turns([
        {"type": "message", "session_id": "s1", "content": "email a"},
        {"type": "decision", "session_id": "s1", "decisions": [{"type": "edit"}]},
        {"type": "decision", "session_id": "s1", "decisions": [{"type": "approve"}]},
    ])
    assert any(f["type"] == "interrupt" for f in turns[0])
    bad = turns[1]
    assert [f["type"] for f in bad] == ["error", "turn_end"], bad  # no ack, nothing sent
    assert "edited_action" in bad[0]["error"]
    good = turns[2]
    assert good[0]["type"] == "ack"
    assert not any(f["type"] == "error" for f in good), good
    assert "Done." in json.dumps(good)
