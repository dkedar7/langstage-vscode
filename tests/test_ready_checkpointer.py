"""gh #152: the `ready` frame names the served agent's checkpointer and whether it is
durable, so the panel shows its "the agent may not remember" note only when a restart
really lost the agent's memory.

The report is read off the built agent, so it covers the saver turns actually run with:
the agent's own, or the ``InMemorySaver`` core attaches to a graph compiled without one.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver, MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from langstage_vscode.sidecar import _checkpointer_info, main, run
from tests.test_sidecar import _isolate_config

README = Path(__file__).resolve().parent.parent / "README.md"


class SqliteLikeSaver(BaseCheckpointSaver):
    """Stands in for a saver that persists outside the process (``SqliteSaver``,
    ``PostgresSaver``): the report only looks at what kind of saver it is."""


def _graph(checkpointer: Any = None):
    b = StateGraph(MessagesState)
    b.add_node("n", lambda s: {"messages": [AIMessage(content="hi")]})
    b.add_edge(START, "n")
    b.add_edge("n", END)
    return b.compile(checkpointer=checkpointer)


def _ready(graph) -> dict:
    out = io.StringIO()
    run(graph, io.StringIO('{"type": "shutdown"}\n'), out)
    frames = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    assert frames[0]["type"] == "ready", frames
    return frames[0]


def test_a_graph_without_a_checkpointer_reports_the_attached_in_memory_one():
    assert _ready(_graph())["checkpointer"] == {"kind": "InMemorySaver", "durable": False}


@pytest.mark.parametrize("saver", [MemorySaver(), InMemorySaver()])
def test_the_agents_own_in_memory_saver_is_not_durable(saver):
    assert _ready(_graph(saver))["checkpointer"] == {"kind": "InMemorySaver", "durable": False}


def test_a_persistent_saver_is_durable():
    assert _ready(_graph(SqliteLikeSaver()))["checkpointer"] == {
        "kind": "SqliteLikeSaver", "durable": True,
    }


def test_a_graph_that_cannot_be_served_gets_a_bare_ready_then_the_error():
    out = io.StringIO()
    run(lambda: None, io.StringIO(""), out)
    frames = [json.loads(ln) for ln in out.getvalue().splitlines() if ln.strip()]
    assert frames[0] == {"type": "ready"}
    assert frames[1]["type"] == "error" and "not a runnable graph" in frames[1]["error"]


class _Agent:
    def __init__(self, saver: Any) -> None:
        self.graph = type("G", (), {"checkpointer": saver})()


class CustomMemoryCheckpointer(BaseCheckpointSaver):
    pass


@pytest.mark.parametrize(
    ("saver", "expected"),
    [
        (None, {"kind": None, "durable": False}),
        (True, {"kind": None, "durable": False}),
        (False, {"kind": None, "durable": False}),
        (CustomMemoryCheckpointer(), {"kind": "CustomMemoryCheckpointer", "durable": False}),
        (type("MySaver", (InMemorySaver,), {})(), {"kind": "MySaver", "durable": False}),
        (SqliteLikeSaver(), {"kind": "SqliteLikeSaver", "durable": True}),
    ],
)
def test_checkpointer_info_classifies_savers(saver, expected):
    assert _checkpointer_info(_Agent(saver)) == expected


def test_selfcheck_json_carries_the_checkpointer(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--selfcheck", "--json"]) == 0
    verdict = json.loads(capsys.readouterr().out.strip())
    assert verdict["checkpointer"] == {"kind": "InMemorySaver", "durable": False}


def test_the_readme_documents_the_field():
    text = README.read_text(encoding="utf-8")
    assert '{"type": "ready", "checkpointer": {"kind":' in text
