"""``--repl`` answers every action of a multi-tool interrupt (gh #184).

When the model makes parallel tool calls, ``HumanInTheLoopMiddleware`` asks about all
of them in one interrupt, and core wants one decision per action. ``--repl`` used to
send exactly one decision, so every answer was refused ("2 action(s) are waiting for a
decision but 1 were sent") and the session could only end with ``:quit`` (exit 2).

Now a verb answers every action the same way, as the panel and ``@langstage`` do, and a
JSON list answers each action in order. Both are checked with core's own
``decision_problems`` before anything is sent, so an answer core would refuse is
refused inline and the interrupt stays pending.
"""
import json
import os
import subprocess
import sys

from langstage_vscode.sidecar import (
    _decision_refusal_notice,
    _format_interrupt_notice,
    _interrupt_action_count,
    _repl_decisions,
)

# The issue's agent: a fake model that makes two parallel tool calls, then replies.
_AGENT_SRC = '''
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool


@tool
def delete_file(path: str) -> str:
    """Delete a file."""
    return f"deleted {path}"


@tool
def send_note(text: str) -> str:
    """Send a note."""
    return "sent"


class M(BaseChatModel):
    @property
    def _llm_type(self):
        return "fake"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if results:
            msg = AIMessage(content="All done: " + " / ".join(str(m.content) for m in results))
        else:
            msg = AIMessage(content="", tool_calls=[
                {"name": "delete_file", "args": {"path": "x.txt"}, "id": "c1"},
                {"name": "send_note", "args": {"text": "hi"}, "id": "c2"}])
        return ChatResult(generations=[ChatGeneration(message=msg)])


graph = create_agent(M(), [delete_file, send_note], middleware=[
    HumanInTheLoopMiddleware(interrupt_on=%(interrupt_on)s)])
'''

_BOTH = '{"delete_file": True, "send_note": True}'
_MIXED = (
    '{"delete_file": True,'
    ' "send_note": {"allowed_decisions": ["approve", "reject"]}}'
)


def _frame(send_note_allows=("approve", "edit", "reject", "respond")):
    """A two-action interrupt frame as core puts it on the wire."""
    every = ["approve", "edit", "reject", "respond"]
    return {
        "type": "interrupt",
        "action_requests": [
            {"name": "delete_file", "args": {"path": "x.txt"}},
            {"name": "send_note", "args": {"text": "hi"}},
        ],
        "review_configs": [
            {"action_name": "delete_file", "allowed_decisions": every},
            {"action_name": "send_note", "allowed_decisions": list(send_note_allows)},
        ],
        "allowed_decisions": every,
    }


def _repl(tmp_path, stdin, interrupt_on=_BOTH):
    (tmp_path / "hitl.py").write_text(
        _AGENT_SRC % {"interrupt_on": interrupt_on}, encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGSTAGE_", "DEEPAGENT"))}
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--agent", "./hitl.py:graph", "--repl"],
        input=stdin, capture_output=True, text=True, env=env, cwd=tmp_path, timeout=180,
    )


# -- the decisions list -------------------------------------------------------


def test_a_verb_answers_every_action():
    frame = _frame()
    assert _interrupt_action_count(frame) == 2
    assert _repl_decisions("approve", frame) == ([{"type": "approve"}, {"type": "approve"}], None)
    decisions, _ = _repl_decisions("reject not now", frame)
    assert decisions == [{"type": "reject", "message": "not now"}] * 2
    # Copies, not one dict shared by every action.
    assert decisions[0] is not decisions[1]


def test_one_action_still_gets_one_decision():
    frame = _frame()
    frame["action_requests"] = frame["action_requests"][:1]
    frame["review_configs"] = frame["review_configs"][:1]
    assert _repl_decisions("approve", frame) == ([{"type": "approve"}], None)
    # A frame with no action_requests at all (a custom interrupt) takes one.
    assert _interrupt_action_count({"allowed_decisions": ["approve"]}) == 1


def test_a_verb_one_action_does_not_allow_is_refused_up_front():
    frame = _frame(send_note_allows=("approve", "reject"))
    decisions, reason = _repl_decisions('edit {"edited_action": {"name": "delete_file", "args": {}}}', frame)
    assert decisions is None
    assert "send_note" in reason and "not allowed" in reason
    assert "JSON list" in reason and ":decision [" in reason


def test_a_json_list_answers_each_action_in_order():
    frame = _frame(send_note_allows=("approve", "reject"))
    answer = '[{"type": "approve"}, {"type": "reject", "message": "no"}]'
    assert _repl_decisions(answer, frame) == (
        [{"type": "approve"}, {"type": "reject", "message": "no"}], None
    )


def test_a_json_list_of_the_wrong_length_is_refused():
    decisions, reason = _repl_decisions('[{"type": "approve"}]', _frame())
    assert decisions is None
    assert reason == "2 action(s) are waiting but the list has 1 decision(s); give one per action, in order"


def test_a_bad_json_list_is_refused():
    for answer, expect in [
        ("[approve, reject]", "not valid JSON"),
        ("[]", "non-empty JSON list"),
        ('[{"type": "approve"}, "reject"]', 'decision 2 must be an object with a string "type"'),
        ('[{"type": "approve"}, {"type": "nope"}]', "decision 2: `nope` is not a decision"),
        ('[{"type": "approve"}, {"type": "respond"}]', 'respond needs a text "message"'),
    ]:
        decisions, reason = _repl_decisions(answer, _frame())
        assert decisions is None, answer
        assert expect in reason, (answer, reason)


# -- what the user reads -------------------------------------------------------


def test_the_repl_notice_explains_both_forms_for_several_actions():
    notice = _format_interrupt_notice(_frame(), repl=True)
    assert "one verb answers all 2 actions" in notice
    assert ':decision [{"type": "approve"}, {"type": "reject"}]' in notice
    assert notice.isascii()
    one = _frame()
    one["action_requests"] = one["action_requests"][:1]
    one["review_configs"] = one["review_configs"][:1]
    assert "answers all" not in _format_interrupt_notice(one, repl=True)
    # The one-shot --message notice can't answer, so it doesn't offer either form.
    assert "answers all" not in _format_interrupt_notice(_frame())


def test_the_refusal_hint_never_echoes_raw_control_characters_from_the_frame():
    allowed = ["approve", "evil\x1b[2J\nallowed: everything"]
    notice = _decision_refusal_notice("x", "nope", allowed, pending=True, actions=2)
    assert "\x1b" not in notice
    assert "\nallowed: everything" not in notice
    assert "a verb answers all 2 actions" in notice


def test_a_refusal_reason_never_echoes_raw_control_characters():
    for answer in ("bad\x1b[31m", '[{"type": "approve"}, {"type": "bad\\u001b[31m"}]'):
        decisions, reason = _repl_decisions(answer, _frame())
        assert decisions is None, answer
        assert "\x1b" not in reason, reason
    # A tool name from the agent, quoted back by core's check, is kept to one line too.
    frame = _frame(send_note_allows=("approve",))
    frame["action_requests"][1]["name"] = "send\x1b[2J\nnote"
    frame["review_configs"][1]["action_name"] = "send\x1b[2J\nnote"
    _, reason = _repl_decisions("reject", frame)
    assert reason is not None and "\x1b" not in reason and "\n" not in reason, reason


# -- end to end: the issue's repro ---------------------------------------------


def test_repl_approve_resumes_a_two_tool_interrupt(tmp_path):
    proc = _repl(tmp_path, "do it\napprove\n:quit\n")
    assert proc.returncode == 0, proc.stderr
    assert "waiting for a decision but 1 were sent" not in proc.stderr
    assert "deleted x.txt" in proc.stdout and "sent" in proc.stdout, proc.stdout


def test_repl_decision_list_answers_each_tool(tmp_path):
    answer = ':decision [{"type": "approve"}, {"type": "reject", "message": "not now"}]'
    proc = _repl(tmp_path, f"do it\n{answer}\n:quit\n")
    assert proc.returncode == 0, proc.stderr
    assert "deleted x.txt" in proc.stdout, proc.stdout
    assert "not now" in proc.stdout or "reject" in proc.stdout.lower(), proc.stdout


def test_repl_refuses_a_verb_one_tool_disallows_then_accepts_approve(tmp_path):
    edit = ':decision edit {"edited_action": {"name": "delete_file", "args": {"path": "y.txt"}}}'
    proc = _repl(tmp_path, f"do it\n{edit}\napprove\n:quit\n", interrupt_on=_MIXED)
    assert proc.returncode == 0, proc.stderr
    assert "not a decision:" in proc.stderr and "send_note" in proc.stderr, proc.stderr
    assert "deleted x.txt" in proc.stdout, proc.stdout


def test_repl_json_mode_sends_one_decision_per_action(tmp_path):
    (tmp_path / "hitl.py").write_text(_AGENT_SRC % {"interrupt_on": _BOTH}, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGSTAGE_", "DEEPAGENT"))}
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--agent", "./hitl.py:graph", "--repl", "--json"],
        input="do it\napprove\n:quit\n", capture_output=True, text=True, env=env,
        cwd=tmp_path, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert not [f for f in frames if f.get("type") == "error"], frames
    assert any(f.get("type") == "complete" for f in frames) or any(
        "All done" in str(f.get("content", "")) for f in frames
    ), frames
