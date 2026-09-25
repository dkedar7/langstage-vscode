"""Decision verbs are validated against the pending interrupt (gh #117, gh #114).

gh #117: the raw stdio ``decision`` handler (the path the extension speaks) never
checked a decision's ``type`` against the pending interrupt's ``allowed_decisions``,
so a verb the interrupt forbids was ACKed and resumed the graph. Only ``--repl``
refused it. gh #114: ``allowed_decisions`` used to be the default four whatever the
interrupt's ``config`` said.

Both drivers now resolve a verb with core's ``normalize_decision`` (langstage-core
>= 1.0.37): canonical verbs and the legacy aliases (``accept`` / ``ignore`` /
``response``) are accepted when the interrupt allows them, and forwarded under the
canonical name. A disallowed verb gets ``error -> turn_end`` (no ``ack``) and leaves
the interrupt pending.
"""
import io
import json

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from typing_extensions import Annotated, TypedDict

from langstage_core import load_agent_spec
from langstage_vscode.sidecar import _run_repl, run


class _S(TypedDict):
    messages: Annotated[list, add_messages]


def _approve_only_graph():
    """gh #114's repro: a standard HumanInterrupt list whose config allows only accept."""
    def node(state: _S):
        answer = interrupt([{
            "action_request": {"action": "delete_database", "args": {"name": "prod"}},
            "config": {"allow_accept": True, "allow_edit": False,
                       "allow_respond": False, "allow_ignore": False},
            "description": "Approve deleting the prod database?",
        }])
        return {"messages": [AIMessage(content=f"resumed with: {answer}")]}

    g = StateGraph(_S)
    g.add_node("respond", node)
    g.add_edge(START, "respond")
    g.add_edge("respond", END)
    return g.compile(checkpointer=MemorySaver())


def _demo_tools():
    """gh #117's repro: ``--demo=tools``'s "ask me first" advertises [respond, approve]."""
    return load_agent_spec("langstage_core.demo.tools:graph")


def _drive(graph, commands):
    stdin = io.StringIO("".join(json.dumps(c) + "\n" for c in commands))
    stdout = io.StringIO()
    run(graph, stdin, stdout)
    return [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]


def _turns(frames):
    """Split frames after ``ready`` into per-turn lists at each ``turn_end``."""
    turns, cur = [], []
    for f in frames:
        if f["type"] == "ready":
            continue
        cur.append(f)
        if f["type"] == "turn_end":
            turns.append(cur)
            cur = []
    return turns


def _msg(content, sid="s1"):
    return {"type": "message", "session_id": sid, "content": content}


def _decide(*verbs, sid="s1"):
    return {"type": "decision", "session_id": sid,
            "decisions": [{"type": v} for v in verbs]}


def _text(turn):
    return "".join(f.get("content", "") for f in turn if f["type"] == "content")


# ── gh #114: allowed_decisions reflects the interrupt's config ─────────────────────


def test_114_allowed_decisions_follow_the_interrupt_config():
    frames = _drive(_approve_only_graph(), [_msg("go")])
    (intr,) = [f for f in frames if f["type"] == "interrupt"]
    assert intr["allowed_decisions"] == ["approve"]


def test_114_demo_tools_advertises_its_restricted_set():
    frames = _drive(_demo_tools(), [_msg("ask me first")])
    (intr,) = [f for f in frames if f["type"] == "interrupt"]
    assert sorted(intr["allowed_decisions"]) == ["approve", "respond"]


# ── gh #117: the raw stdio decision handler enforces allowed_decisions ─────────────


def test_117_disallowed_verb_is_refused_and_the_interrupt_stays_pending():
    frames = _drive(_demo_tools(), [
        _msg("ask me first"),
        _decide("reject"),      # not in [respond, approve]
        _decide("approve"),     # still pending, so this answers it
    ])
    first, refused, answered = _turns(frames)
    assert any(f["type"] == "interrupt" for f in first)
    assert [f["type"] for f in refused] == ["error", "turn_end"]  # no ack, nothing ran
    assert "reject" in refused[0]["error"]
    assert "approve" in refused[0]["error"] and "respond" in refused[0]["error"]
    assert refused[1]["session_id"] == "s1"
    assert answered[0] == {"type": "ack", "ref": "decision"}
    assert "'type': 'approve'" in _text(answered)


def test_117_edit_is_refused_on_an_approve_only_interrupt():
    frames = _drive(_approve_only_graph(), [
        _msg("go"),
        {"type": "decision", "session_id": "s1",
         "decisions": [{"type": "edit", "edited_action": {"name": "x", "args": {}}}]},
    ])
    _, refused = _turns(frames)
    assert [f["type"] for f in refused] == ["error", "turn_end"]
    assert "edit" in refused[0]["error"]


def test_117_one_disallowed_verb_refuses_the_whole_decision_list():
    frames = _drive(_demo_tools(), [_msg("ask me first"), _decide("approve", "edit")])
    _, refused = _turns(frames)
    assert [f["type"] for f in refused] == ["error", "turn_end"]
    assert "edit" in refused[0]["error"]


@pytest.mark.parametrize("verb", ["accept", "ACCEPT", "Approve"])
def test_117_aliases_and_case_are_accepted_and_forwarded_canonically(verb):
    frames = _drive(_demo_tools(), [_msg("ask me first"), _decide(verb)])
    _, answered = _turns(frames)
    assert answered[0] == {"type": "ack", "ref": "decision"}
    assert "'type': 'approve'" in _text(answered)


def test_117_alias_for_a_disallowed_verb_is_refused():
    # `ignore` means `reject`, which the demo interrupt does not allow.
    frames = _drive(_demo_tools(), [_msg("ask me first"), _decide("ignore")])
    _, refused = _turns(frames)
    assert [f["type"] for f in refused] == ["error", "turn_end"]


def test_117_accept_answers_an_approve_only_interrupt():
    frames = _drive(_approve_only_graph(), [_msg("go"), _decide("accept")])
    _, answered = _turns(frames)
    assert answered[0] == {"type": "ack", "ref": "decision"}
    assert "resumed with:" in _text(answered)


@pytest.mark.parametrize("decision", [{}, {"type": 5}, "approve", {"type": ""}])
def test_117_a_decision_without_a_verb_is_refused(decision):
    frames = _drive(_demo_tools(), [
        _msg("ask me first"),
        {"type": "decision", "session_id": "s1", "decisions": [decision]},
    ])
    _, refused = _turns(frames)
    assert [f["type"] for f in refused] == ["error", "turn_end"]


# ── --repl and stdio agree ─────────────────────────────────────────────────────────


def _repl(graph, lines):
    stdin = io.StringIO("".join(line + "\n" for line in lines))
    out, err = io.StringIO(), io.StringIO()
    rc = _run_repl(graph, spec=None, as_json=False, stdin=stdin, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def test_repl_accepts_an_alias_and_sends_the_canonical_verb():
    rc, out, err = _repl(_demo_tools(), ["ask me first", "accept", ":quit"])
    assert rc == 0, err
    assert "'type': 'approve'" in out


def test_repl_refuses_a_disallowed_verb_like_stdio():
    rc, out, err = _repl(_demo_tools(), ["ask me first", "reject", ":quit"])
    assert "not a decision this interrupt allows" in err
    assert rc == 2  # still pending when the session ended


def test_parse_repl_decision_uses_core_normalization():
    from langstage_vscode.sidecar import _parse_repl_decision

    allowed = ["respond", "approve"]
    assert _parse_repl_decision("accept", allowed) == ({"type": "approve"}, None)
    assert _parse_repl_decision("response ok", allowed) == (
        {"type": "respond", "message": "ok"}, None)
    decision, err = _parse_repl_decision("ignore", allowed)
    assert decision is None and "not a decision this interrupt allows" in err
