"""Deferred-backlog pass: cancel rollback (gh #106), core version in --version (gh #132),
and the decision round-trip the VS Code chat panel now drives (HITL from the chat)."""
import asyncio
import json
import threading
import time
from typing import List

import pytest

pytest.importorskip("ag_ui_langgraph")

from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.types import interrupt  # noqa: E402

from langstage_vscode.sidecar import main, run  # noqa: E402


# ── gh #106: a cancelled turn leaves no orphaned human message ────────────────


def _role_graph():
    """Replies with the roles it sees in state. Slow (cooperatively cancellable) only
    on a message containing ``slow``, so that turn can be cancelled mid-flight."""

    async def respond(state):
        last = str(state["messages"][-1].content) if state["messages"] else ""
        if "slow" in last:
            for _ in range(100):
                await asyncio.sleep(0.05)
        roles = ",".join(m.type for m in state["messages"])
        return {"messages": [AIMessage(content=f"roles={roles}")]}

    b = StateGraph(MessagesState)
    b.add_node("respond", respond)
    b.add_edge(START, "respond")
    b.add_edge("respond", END)
    return b.compile(checkpointer=InMemorySaver())


def _drive_with_cancel(graph, prompts: List[str]) -> List[dict]:
    """Send ``prompts`` in order on one session over the raw stdio path, cancelling
    every prompt containing ``slow`` shortly after its ``ack``. Each prompt waits for
    the previous turn's ``turn_end``, like the extension does."""
    frames: List[dict] = []
    turn_ended = threading.Semaphore(0)
    acked = threading.Semaphore(0)

    class _Stdin:
        def __iter__(self):
            for p in prompts:
                yield json.dumps({"type": "message", "session_id": "s", "content": p})
                if "slow" in p:
                    acked.acquire(timeout=15)
                    time.sleep(0.3)
                    yield json.dumps({"type": "cancel", "session_id": "s"})
                turn_ended.acquire(timeout=15)
            yield json.dumps({"type": "shutdown"})

    class _Stdout:
        def write(self, s: str) -> None:
            for line in s.splitlines():
                if line.strip():
                    f = json.loads(line)
                    frames.append(f)
                    if f["type"] == "ack":
                        acked.release()
                    elif f["type"] == "turn_end":
                        turn_ended.release()

        def flush(self) -> None:
            pass

    run(graph, _Stdin(), _Stdout(), enable_cancel=True)
    return frames


def _replies(frames: List[dict]) -> List[str]:
    return [f["content"] for f in frames if f["type"] == "content"]


def test_cancel_mid_conversation_leaves_clean_alternation():
    frames = _drive_with_cancel(_role_graph(), ["one", "slow two", "three"])
    assert "cancelled" in [f["type"] for f in frames]
    # Turn 3 sees turn 1's pair and its own message: the cancelled prompt is gone.
    assert _replies(frames)[-1] == "roles=human,ai,human", _replies(frames)


def test_cancel_of_the_first_turn_leaves_an_empty_thread():
    frames = _drive_with_cancel(_role_graph(), ["slow one", "two"])
    assert "cancelled" in [f["type"] for f in frames]
    assert _replies(frames)[-1] == "roles=human", _replies(frames)


def test_control_run_without_cancel_is_unchanged():
    frames = _drive_with_cancel(_role_graph(), ["one", "two", "three"])
    assert _replies(frames)[-1] == "roles=human,ai,human,ai,human"


# ── gh #132: --version names the langstage-core runtime ───────────────────────


def test_version_reports_core(capsys):
    from importlib.metadata import version

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert f"langstage-core {version('langstage-core')}" in out


# ── HITL from the chat panel: interrupt -> decision -> resumed turn ───────────


def _approval_graph():
    """Asks for approval of a tool call (HumanInTheLoopMiddleware shape), then
    reports the decision it got back as its reply."""

    def ask(state):
        answer = interrupt({
            "action_requests": [{"name": "delete_file", "args": {"path": "x.txt"},
                                 "description": "Delete x.txt?"}],
            "review_configs": [{"action_name": "delete_file",
                                "allowed_decisions": ["approve", "reject", "respond"]}],
        })
        return {"messages": [AIMessage(content=f"got={json.dumps(answer, sort_keys=True)}")]}

    b = StateGraph(MessagesState)
    b.add_node("ask", ask)
    b.add_edge(START, "ask")
    b.add_edge("ask", END)
    return b.compile(checkpointer=InMemorySaver())


def _drive_turns(graph, commands: List[dict]) -> List[List[dict]]:
    """Run ``commands`` one turn at a time; return each turn's frames."""
    import io

    stdin = io.StringIO("".join(json.dumps(c) + "\n" for c in commands)
                        + json.dumps({"type": "shutdown"}) + "\n")
    stdout = io.StringIO()
    run(graph, stdin, stdout)
    turns: List[List[dict]] = [[]]
    for line in stdout.getvalue().splitlines():
        if not line.strip():
            continue
        f = json.loads(line)
        if f["type"] == "ready":
            continue
        turns[-1].append(f)
        if f["type"] == "turn_end":
            turns.append([])
    return [t for t in turns if t]


@pytest.mark.parametrize(
    "decision",
    [
        {"type": "approve"},
        {"type": "reject", "message": "not now"},
        {"type": "respond", "message": "use y.txt instead"},
    ],
)
def test_each_chat_decision_resumes_the_same_session(decision):
    turns = _drive_turns(_approval_graph(), [
        {"type": "message", "session_id": "vscode-abc", "content": "clean up"},
        {"type": "decision", "session_id": "vscode-abc", "decisions": [decision]},
    ])
    first, resumed = turns
    interrupt_frame = next(f for f in first if f["type"] == "interrupt")
    assert set(interrupt_frame["allowed_decisions"]) == {"approve", "reject", "respond"}
    assert [f["type"] for f in resumed][:1] == ["ack"]
    reply = "".join(f["content"] for f in resumed if f["type"] == "content")
    assert json.loads(reply.split("got=", 1)[1]) == {"decisions": [decision]}
    assert resumed[-1] == {"type": "turn_end", "session_id": "vscode-abc"}


def test_a_verb_the_interrupt_does_not_allow_is_refused_and_stays_pending():
    turns = _drive_turns(_approval_graph(), [
        {"type": "message", "session_id": "s", "content": "clean up"},
        {"type": "decision", "session_id": "s", "decisions": [{"type": "edit"}]},
        {"type": "decision", "session_id": "s", "decisions": [{"type": "approve"}]},
    ])
    refused, accepted = turns[1], turns[2]
    assert [f["type"] for f in refused] == ["error", "turn_end"]
    assert "not allowed" in refused[0]["error"]
    assert accepted[0]["type"] == "ack"


# ── gh #97: --help names the --demo=tools trigger phrases ─────────────────────


def test_help_names_the_demo_tools_trigger_phrases(capsys):
    from langstage_core.demo import tools as demo_tools

    with pytest.raises(SystemExit):
        main(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())  # undo argparse wrapping
    for phrase in (demo_tools.TOOL_TRIGGER, demo_tools.REASONING_TRIGGER,
                   demo_tools.INTERRUPT_TRIGGER):
        assert f"'{phrase}'" in help_text, phrase
