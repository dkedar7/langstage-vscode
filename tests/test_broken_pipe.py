"""A closed stdout ends the sidecar quietly (gh #163).

Piping the `--json` frame stream into a reader that stops early (`| head -4`), or a
client that goes away, closes stdout mid-turn. The turn's `except Exception` took the
failed write for an agent error and wrote an `error` frame to the same dead pipe, so
the process died with a double BrokenPipeError traceback and exit 1.
"""

import subprocess
import sys

import pytest

pytest.importorskip("ag_ui_langgraph")


def test_reader_closing_early_exits_quietly():
    proc = subprocess.Popen(
        [sys.executable, "-m", "langstage_vscode", "--demo=tools", "--message", "think about it", "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    first = proc.stdout.readline()  # like `head -1`: read one frame, then stop reading
    assert first.strip()
    proc.stdout.close()
    _, err = proc.communicate(timeout=120)
    err = err.decode("utf-8", "replace")
    assert "Traceback" not in err, err
    assert "BrokenPipeError" not in err, err
    assert proc.returncode == 0, (proc.returncode, err)
