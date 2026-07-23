"""Packaging guards.

gh #60: ``[project]`` had no ``readme`` key, so hatchling never set a long
description and <https://pypi.org/project/langstage-vscode/> rendered a blank
page — the natural first stop for an adopter carried no install command, no
Quickstart, no sidecar-protocol table. Every sibling LangStage stage ships its
README to PyPI; this one silently did not, and nothing in the test suite noticed
because the omission only manifests in built distribution metadata.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
REPO = PYPROJECT.parent


def _project() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]


def _norm(text: str) -> str:
    """Normalize a doc trace to compare it regardless of arrow glyph (README uses
    ``->`` rendered as ``→``; the CHANGELOG uses ``->``) or surrounding whitespace."""
    return text.replace("→", "->").replace("  ", " ")


def test_project_declares_a_readme():
    """Without this key the wheel ships no long description at all (gh #60)."""
    assert "readme" in _project(), (
        "[project] must declare `readme` — otherwise the PyPI project page is "
        "blank and the shipped wheel has no Description-Content-Type (gh #60)"
    )


def test_declared_readme_exists_and_has_content():
    """A `readme` pointing at a missing/stub file would be just as blank."""
    readme = PYPROJECT.parent / _project()["readme"]
    assert readme.is_file(), f"declared readme {readme.name} does not exist"
    # The real README is ~12 KB; guard against it degrading to a stub.
    assert len(readme.read_text(encoding="utf-8").strip()) > 1000


# ── gh #66: the documented interrupt->decision trace must include both `complete`s ──
#
# The runtime emits a `complete` after BOTH the `interrupt` turn and the resumed
# `content` turn (pinned behaviorally in test_sidecar.py::
# test_interrupt_decision_trace_emits_complete_on_both_turns). The shipped README and
# CHANGELOG had regressed to dropping the two `complete` frames — and the CHANGELOG
# wrongly called the truncated trace "identical to the hand-written raw-protocol
# transcript". These guard the docs against that regression returning.

# The real runtime sequence (normalized to `->` arrows). Both `complete`s present.
_CORRECT = _norm(
    "ready -> ack message -> interrupt -> complete -> turn_end "
    "-> ack decision -> content -> complete -> turn_end"
)
# The buggy trace that dropped both `complete` frames (must appear in NEITHER doc).
_BUGGY = _norm(
    "ready -> ack message -> interrupt -> turn_end -> ack decision -> content -> turn_end"
)


def test_readme_interrupt_decision_trace_has_both_complete_frames():
    text = _norm((REPO / "README.md").read_text(encoding="utf-8"))
    assert _CORRECT in text, "README must document the trace WITH both `complete` frames (gh #66)"
    assert _BUGGY not in text, "README still carries the old trace missing the `complete` frames (gh #66)"


def test_changelog_interrupt_decision_trace_has_both_complete_frames():
    text = _norm((REPO / "CHANGELOG.md").read_text(encoding="utf-8"))
    # The corrected trace must be documented — it appears in both the fixed 0.5.18 line
    # and the 0.5.19 entry. (The buggy trace is deliberately NOT forbidden here: the
    # 0.5.19 entry quotes it exactly once, inside prose that explains what regressed and
    # that it was corrected — so a blanket "buggy string absent" check would wrongly
    # forbid honest changelog history. The behavioral anchor in test_sidecar.py keeps
    # the trace itself true.)
    assert _CORRECT in text, "CHANGELOG must document the trace WITH both `complete` frames (gh #66)"
