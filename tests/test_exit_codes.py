"""The LangStage family exit codes (core ADR 0007): 0 ok / 1 failed / 2 paused on a HITL
interrupt / 64 usage error. argparse's own usage code is 2, which would read as "paused",
so the sidecar's parser exits 64 instead.
"""
import json

import pytest

from langstage_vscode import sidecar
from langstage_vscode.sidecar import main
from tests.test_sidecar import _isolate_config


def test_constants_match_the_family_scheme():
    assert (sidecar.EXIT_OK, sidecar.EXIT_FAIL, sidecar.EXIT_PAUSED, sidecar.EXIT_USAGE) == (
        0, 1, 2, 64,
    )
    assert sidecar.SELFCHECK_PAUSED_EXIT == sidecar.EXIT_PAUSED


# ---- 64: usage errors -------------------------------------------------------------


@pytest.mark.parametrize("argv", [["--demo=bogus"], ["--bogus"], ["--message"]])
def test_argparse_usage_errors_exit_64(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 64
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--demo", "--agent", "x.py:g"],
        ["--demo=tools", "--agent", "x.py:g", "--selfcheck"],
        ["--demo", "--agent", "x.py:g", "--message", "hi"],
        ["--demo", "--repl", "--message", "hi"],
        ["--demo", "--repl", "--message", "hi", "--json"],
    ],
)
def test_conflicting_flags_exit_64(argv, monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(argv) == 64
    captured = capsys.readouterr()
    assert "mutually exclusive" in captured.out + captured.err


def test_help_and_version_exit_0(capsys):
    for argv in (["--help"], ["--version"]):
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0


# ---- 1: failures ------------------------------------------------------------------


@pytest.mark.parametrize("extra", [[], ["--message", "hi"], ["--repl"]])
def test_no_spec_exits_1(extra, monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(extra) == 1


@pytest.mark.parametrize("extra", [[], ["--message", "hi"], ["--selfcheck"]])
def test_load_failure_exits_1(extra, monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--agent", "./nope.py:graph", *extra]) == 1


def test_selfcheck_failure_exits_1(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    (tmp_path / "bad.py").write_text("graph = 42\n")
    assert main(["--agent", "./bad.py:graph", "--selfcheck", "--json"]) == 1
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ok"] is False


# ---- 2: paused / 0: ok -------------------------------------------------------------


def test_selfcheck_ok_0_and_message_ok_0(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo", "--selfcheck"]) == 0
    assert main(["--demo", "--message", "hi"]) == 0
    assert main(["--show-config"]) == 0


def test_message_interrupt_exits_2(monkeypatch, tmp_path, capsys):
    _isolate_config(monkeypatch, tmp_path)
    assert main(["--demo=tools", "--message", "ask me first", "--json"]) == 2
