"""gh #174: `--message` and `--repl` get a fresh thread per invocation.

With a constant session id ("once", "repl") an agent compiled with a durable
checkpointer replayed earlier runs' history into each new run, and one run left paused
on an interrupt made every later `--message` exit 2 (same bug as langstage-core #221).
A shared checkpointer instance stands in for the durable one: it outlives the runs the
way a database file does.
"""
import io
import json

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from langstage_vscode.sidecar import _fresh_session, _run_once, _run_repl


def _echo_graph(saver):
    """Replies with the human messages its thread has seen; pauses on "ask me"."""
    def node(state):
        if "ask me" in state["messages"][-1].content:
            interrupt({"action_request": {"action": "confirm", "args": {}}, "description": "ok?"})
        humans = [m.content for m in state["messages"] if m.type == "human"]
        return {"messages": [AIMessage(content=f"seen {humans}")]}

    b = StateGraph(MessagesState)
    b.add_node("n", node)
    b.add_edge(START, "n")
    b.add_edge("n", END)
    return b.compile(checkpointer=saver)


def _once(graph, message, capsys):
    rc = _run_once(graph, message, spec=None, as_json=True)
    frames = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    text = "".join(f.get("content", "") for f in frames if f["type"] == "content")
    return rc, text, frames


def test_each_message_run_sees_only_its_own_history(capsys):
    graph = _echo_graph(MemorySaver())
    assert _once(graph, "one", capsys)[:2] == (0, "seen ['one']")
    assert _once(graph, "two", capsys)[:2] == (0, "seen ['two']")


def test_a_paused_run_does_not_trap_later_message_runs(capsys):
    graph = _echo_graph(MemorySaver())
    assert _once(graph, "ask me", capsys)[0] == 2
    assert _once(graph, "three", capsys)[:2] == (0, "seen ['three']")


def test_message_runs_use_distinct_once_threads(capsys):
    graph = _echo_graph(MemorySaver())
    ids = []
    for m in ("a", "b"):
        frames = _once(graph, m, capsys)[2]
        ids += [f["session_id"] for f in frames if f["type"] == "turn_end"]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert all(i.startswith("once-") for i in ids)


def _repl(graph, lines):
    out = io.StringIO()
    rc = _run_repl(graph, spec=None, as_json=True, stdin=io.StringIO("".join(l + "\n" for l in lines)),
                   stdout=out, stderr=io.StringIO(), show_prompt=False)
    frames = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    return rc, [f["content"] for f in frames if f["type"] == "content"]


def test_a_new_repl_starts_a_new_conversation_but_remembers_within_itself():
    graph = _echo_graph(MemorySaver())
    assert _repl(graph, ["alpha", "beta"]) == (0, ["seen ['alpha']", "seen ['alpha', 'beta']"])
    assert _repl(graph, ["gamma"]) == (0, ["seen ['gamma']"])


def test_an_explicit_repl_session_id_is_kept():
    graph = _echo_graph(MemorySaver())
    out = io.StringIO()
    _run_repl(graph, spec=None, as_json=True, stdin=io.StringIO("hi\n"), stdout=out,
              stderr=io.StringIO(), show_prompt=False, session_id="mine")
    ends = [json.loads(ln) for ln in out.getvalue().splitlines() if '"turn_end"' in ln]
    assert [f["session_id"] for f in ends] == ["mine"]


def test_fresh_session_ids_are_unique():
    assert _fresh_session("once") != _fresh_session("once")
