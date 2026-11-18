"""The interrupt notice names the tool that is waiting, whatever shape named it (gh #182).

``--message`` and ``--repl`` print a notice on stderr when a turn pauses on a
human-in-the-loop interrupt; in ``--repl`` the user answers from that notice. For
LangChain's ``HumanInTheLoopMiddleware`` the notice used to leave out the ``action:``
line: the middleware's ``ActionRequest`` names the tool under ``name`` and the sidecar
read only ``action``. So the user was asked to ``approve`` without being told what.

The name is the agent's own text, so it is also kept to one safe line: it must not be
able to add a notice line, forge an ``allowed:`` segment or send terminal escapes.
"""
import json
import os
import subprocess
import sys

import pytest

from langstage_vscode.sidecar import (
    _format_interrupt_notice,
    _interrupt_action_decisions,
    _interrupt_actions,
    main,
)

_MW_SRC = '''
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
def send_note(to: str) -> str:
    """Send a note."""
    return f"sent to {to}"


class M(BaseChatModel):
    @property
    def _llm_type(self):
        return "fake"

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        if any(isinstance(m, ToolMessage) for m in messages):
            msg = AIMessage(content="done")
        else:
            msg = AIMessage(content="", tool_calls=%(calls)s)
        return ChatResult(generations=[ChatGeneration(message=msg)])


graph = create_agent(M(), [delete_file, send_note], middleware=[
    HumanInTheLoopMiddleware(interrupt_on=%(interrupt_on)s)])
'''

_ONE_CALL = '[{"id": "c1", "name": "delete_file", "args": {"path": "notes/old.txt"}}]'
_TWO_CALLS = (
    '[{"id": "c1", "name": "delete_file", "args": {"path": "notes/old.txt"}},'
    ' {"id": "c2", "name": "send_note", "args": {"to": "a@example.org"}}]'
)


def _isolate(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(("LANGSTAGE_", "DEEPAGENT")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LANGSTAGE_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def _write_agent(tmp_path, calls=_ONE_CALL, interrupt_on='{"delete_file": True}'):
    (tmp_path / "mw.py").write_text(
        _MW_SRC % {"calls": calls, "interrupt_on": interrupt_on}, encoding="utf-8"
    )


def _detail(notice: str) -> str:
    """The notice's second line: ``  action: ...   allowed: ...``."""
    return notice.split("\n")[1]


# ── the shapes ───────────────────────────────────────────────────────────────


def test_middleware_request_is_named_by_its_name_key():
    # HumanInTheLoopMiddleware's ActionRequest, as core puts it on the wire.
    frame = {
        "type": "interrupt",
        "action_requests": [{"name": "delete_file", "args": {"path": "notes/old.txt"},
                             "description": "Tool execution requires approval"}],
        "review_configs": [{"action_name": "delete_file",
                            "allowed_decisions": ["approve", "edit", "reject", "respond"]}],
        "allowed_decisions": ["reject", "edit", "respond", "approve"],
    }
    assert _interrupt_actions(frame) == ["delete_file"]
    notice = _format_interrupt_notice(frame)
    assert _detail(notice) == "  action: delete_file   allowed: reject | edit | respond | approve"
    assert notice.isascii()
    repl = _format_interrupt_notice(frame, repl=True)
    assert _detail(repl) == _detail(notice)
    assert "answer it here" in repl


@pytest.mark.parametrize(
    "request_, expected",
    [
        ({"name": "delete_file", "args": {}}, "delete_file"),              # middleware
        ({"action": "approve_tool", "args": {}}, "approve_tool"),          # HumanInterrupt list
        ({"action_request": {"action": "confirm"}, "description": "?"}, "confirm"),  # nested
        ({"action_request": {"name": "run_sql", "args": {}}}, "run_sql"),  # nested, by name
        ({"tool": "shell", "args": {}}, "shell"),                           # keyed dict
        ({"action_request": {"args": {}}, "tool": "shell"}, "shell"),      # tool beside nested
        ({"action_request": {"args": {}}, "action": "outer"}, "outer"),    # falls to top level
        # the panel reads name before action (extension/src/hitl.ts), so does the notice
        ({"name": "the_tool", "action": "other"}, "the_tool"),
        ({"action_request": {"action": "inner"}, "name": "outer"}, "inner"),  # nested wins
    ],
)
def test_every_shape_the_panel_reads_names_its_action(request_, expected):
    assert _interrupt_actions({"action_requests": [request_]}) == [expected]


def test_odd_entries_contribute_nothing_and_never_raise():
    frame = {"action_requests": [
        {"name": "a"}, "junk", {}, None, 7, {"name": None}, {"name": ""}, {"name": "   "},
        {"name": {"nested": "dict"}}, {"name": ["list"]}, {"action_request": "not a dict"},
        {"action": "b"},
    ]}
    assert _interrupt_actions(frame) == ["a", "b"]
    assert _interrupt_actions({}) == []
    assert _interrupt_actions({"action_requests": None}) == []
    assert _interrupt_actions({"action_requests": "delete_file"}) == []
    assert _interrupt_actions({"action_requests": {"name": "x"}}) == []
    # ...and the notice for a frame that names nothing has no `action:` segment.
    bare = _format_interrupt_notice({"action_requests": [{}], "allowed_decisions": ["approve"]})
    assert _detail(bare) == "  allowed: approve"


def test_a_number_or_bool_name_is_shown_as_text():
    assert _interrupt_actions({"action_requests": [{"name": 42}, {"action": True}]}) == ["42", "True"]


# ── several requests in one interrupt ───────────────────────────────────────


def test_several_requests_are_all_named_in_order():
    frame = {
        "action_requests": [{"name": "delete_file", "args": {}}, {"name": "send_note", "args": {}}],
        "review_configs": [
            {"action_name": "delete_file", "allowed_decisions": ["approve", "reject"]},
            {"action_name": "send_note", "allowed_decisions": ["approve", "reject"]},
        ],
        "allowed_decisions": ["reject", "approve"],
    }
    # Same verbs for both: one `allowed:` list says it all.
    assert _interrupt_action_decisions(frame) == []
    assert _detail(_format_interrupt_notice(frame)) == (
        "  action: delete_file, send_note   allowed: reject | approve"
    )


def test_requests_with_different_verbs_each_show_their_own():
    # The frame's allowed_decisions is the union; on its own it would offer `edit` for
    # delete_file, which only allows approve / reject.
    frame = {
        "action_requests": [{"name": "delete_file", "args": {}}, {"name": "send_note", "args": {}}],
        "review_configs": [
            {"action_name": "delete_file", "allowed_decisions": ["approve", "reject"]},
            {"action_name": "send_note", "allowed_decisions": ["approve", "edit", "reject"]},
        ],
        "allowed_decisions": ["reject", "edit", "approve"],
    }
    assert _interrupt_action_decisions(frame) == [
        ("delete_file", ["approve", "reject"]),
        ("send_note", ["approve", "edit", "reject"]),
    ]
    assert _detail(_format_interrupt_notice(frame)) == (
        "  action: delete_file (approve | reject), send_note (approve | edit | reject)"
        "   allowed: reject | edit | approve"
    )


def test_review_configs_are_matched_by_name_when_not_index_aligned():
    frame = {
        "action_requests": [{"name": "delete_file"}, {"name": "send_note"}],
        "review_configs": [
            {"action_name": "send_note", "allowed_decisions": ["approve", "edit"]},
            {"action_name": "unrelated", "allowed_decisions": ["respond"]},
            {"action_name": "delete_file", "allowed_decisions": ["approve"]},
        ],
        "allowed_decisions": ["approve", "edit"],
    }
    assert _interrupt_action_decisions(frame) == [
        ("delete_file", ["approve"]), ("send_note", ["approve", "edit"]),
    ]


@pytest.mark.parametrize(
    "configs",
    [
        None, [], "junk", [None, None], [{"action_name": "delete_file"}, {}],
        [{"action_name": "delete_file", "allowed_decisions": ["approve"]}],   # one is missing
        [{"allowed_decisions": "approve"}, {"allowed_decisions": ["approve"]}],
    ],
)
def test_incomplete_review_configs_fall_back_to_the_frame_level_list(configs):
    frame = {
        "action_requests": [{"name": "delete_file"}, {"name": "send_note"}],
        "review_configs": configs,
        "allowed_decisions": ["approve", "reject"],
    }
    assert _interrupt_action_decisions(frame) == []
    assert _detail(_format_interrupt_notice(frame)) == (
        "  action: delete_file, send_note   allowed: approve | reject"
    )


# ── the name is untrusted text ──────────────────────────────────────────────


def _one_line_notice(name, **frame):
    notice = _format_interrupt_notice(
        {"action_requests": [{"name": name}], "allowed_decisions": ["approve", "reject"], **frame}
    )
    assert notice.endswith("\n")
    # str.splitlines() is the strictest splitter there is (\n, \r, \v, \f, \x1c-\x1e,
    # \x85, U+2028, U+2029): header, detail, closing line, and never a fourth.
    lines = notice.splitlines()
    assert len(lines) == 3, repr(notice)
    assert lines[0] == "interrupt: agent paused awaiting a decision"
    assert lines[2].startswith("  resume by sending")
    return lines[1]


def test_a_name_cannot_add_notice_lines():
    detail = _one_line_notice(
        "delete_file\n  allowed: approve\ninterrupt: resolved, nothing to approve\r\nok"
    )
    assert detail == (
        '  action: "delete_file allowed: approve interrupt: resolved, nothing to approve ok"'
        "   allowed: approve | reject"
    )


@pytest.mark.parametrize(
    "sep", ["\n", "\r", "\r\n", "\v", "\f", "\x85", "\u2028", "\u2029", "\x1c", "\x1d", "\x1e"]
)
def test_every_line_separator_is_flattened(sep):
    detail = _one_line_notice(f"safe{sep}allowed: approve")
    assert detail == '  action: "safe allowed: approve"   allowed: approve | reject'


def test_a_name_cannot_send_terminal_escapes_or_control_characters():
    detail = _one_line_notice("\x1b[2J\x1b[1;31mrm\x07\x00\x08\x7f\x9b0m_all\x1b]0;title\x07")
    assert not any(ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in detail), repr(detail)
    assert detail == '  action: "[2J [1;31mrm 0m_all ]0;title"   allowed: approve | reject'


def test_a_name_cannot_reorder_or_hide_text_with_format_characters():
    # right-to-left override, zero-width space/joiner, BOM, a lone surrogate, private use
    detail = _one_line_notice("safe\u202e_tool\u200b\u200d\ufeffx\ud800y\ue000z")
    assert detail == '  action: "safe _tool x y z"   allowed: approve | reject'


def test_a_name_with_spaces_or_punctuation_is_quoted_so_it_reads_as_one_name():
    assert _one_line_notice("x   allowed: approve | edit").startswith(
        '  action: "x allowed: approve | edit"   allowed: '
    )
    assert _one_line_notice("a, b").startswith('  action: "a, b"   ')
    assert _one_line_notice('say "hi"\\now').startswith('  action: "say \\"hi\\"\\\\now"   ')
    # ordinary tool names stay bare, as the README shows them
    for plain in ("delete_file", "fs.delete", "mcp:files/read", "tool-1", "ns@v2+x"):
        assert _one_line_notice(plain).startswith(f"  action: {plain}   ")


def test_a_long_name_is_cut_off():
    detail = _one_line_notice("t" * 5000)
    assert detail == "  action: " + "t" * 77 + "...   allowed: approve | reject"
    spaced = _one_line_notice("word " * 1000)
    assert len(spaced) < 130 and spaced.startswith('  action: "word word') and '..."' in spaced


def test_a_non_ascii_name_is_kept_readable_and_quoted():
    assert _one_line_notice("löschen").startswith('  action: "löschen"   ')


def test_verbs_in_the_notice_are_one_line_too():
    notice = _format_interrupt_notice(
        {"action_requests": [{"name": "delete_file"}],
         "allowed_decisions": ["approve", "reject\n  action: harmless", "\x1b[31m", ""]},
        repl=True,
    )
    lines = notice.splitlines()
    assert len(lines) == 4, repr(notice)
    assert lines[1] == "  action: delete_file   allowed: approve | reject action: harmless | [31m"
    assert lines[3].startswith("  payloads: ")
    assert "\x1b" not in notice


# ── through the real sidecar, with the real middleware ──────────────────────


def test_message_names_the_tool_for_a_middleware_interrupt(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _write_agent(tmp_path)
    assert main(["--agent", "./mw.py:graph", "--message", "go"]) == 2
    err = capsys.readouterr().err
    assert "  action: delete_file   allowed: " in err, err
    detail = next(ln for ln in err.splitlines() if "allowed:" in ln)
    assert set(detail.split("allowed:")[1].strip().split(" | ")) == {
        "approve", "edit", "reject", "respond"}


def test_json_frame_is_unchanged_by_the_notice_fix(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _write_agent(tmp_path)
    assert main(["--agent", "./mw.py:graph", "--message", "go", "--json"]) == 2
    out = capsys.readouterr()
    frame = next(f for f in map(json.loads, out.out.splitlines()) if f["type"] == "interrupt")
    assert frame["action_requests"][0]["name"] == "delete_file"
    assert frame["action_requests"][0]["args"] == {"path": "notes/old.txt"}
    assert "action" not in frame["action_requests"][0]     # the wire is not rewritten
    assert "action:" not in out.err                         # --json prints no notice


def test_message_names_each_tool_with_its_own_verbs(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    _write_agent(
        tmp_path, calls=_TWO_CALLS,
        interrupt_on='{"delete_file": {"allowed_decisions": ["approve", "reject"]},'
                     ' "send_note": {"allowed_decisions": ["approve", "edit", "reject"]}}',
    )
    assert main(["--agent", "./mw.py:graph", "--message", "go"]) == 2
    err = capsys.readouterr().err
    assert "action: delete_file (approve | reject), send_note (approve | edit | reject)" in err, err


def test_repl_names_the_tool_and_the_answer_still_resumes(tmp_path):
    _write_agent(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LANGSTAGE_", "DEEPAGENT"))}
    env["LANGSTAGE_CONFIG_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "langstage_vscode", "--agent", "./mw.py:graph", "--repl"],
        input="go\n:decision approve\n:quit\n",
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "  action: delete_file   allowed: " in proc.stderr, proc.stderr
    assert "done" in proc.stdout
