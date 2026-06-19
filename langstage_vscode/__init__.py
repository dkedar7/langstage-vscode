"""langstage-vscode — Python stdio sidecar for the VS Code chat extension.

The extension spawns ``python -m langstage_vscode`` (or the
``langstage-vscode-sidecar`` console script); the sidecar bridges a
LangGraph/deepagents agent to the chat participant over newline-delimited JSON.
"""
from importlib.metadata import PackageNotFoundError, version

from .sidecar import main, run

# Single source of truth is the installed distribution metadata, so this can
# never drift from pyproject's version (it was stuck at a stale "0.1.0").
try:
    __version__ = version("langstage-vscode")
except PackageNotFoundError:  # pragma: no cover - editable/source checkout
    __version__ = "0.0.0+local"

__all__ = ["main", "run", "__version__"]
