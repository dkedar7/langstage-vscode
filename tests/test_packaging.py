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


def _project() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]


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
