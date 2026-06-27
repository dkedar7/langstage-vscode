"""Deprecated alias package: ``deepagent_vscode`` is now ``langstage_vscode``.

Kept for one transition window so existing imports and spawn commands keep
working. Import / spawn ``langstage_vscode`` instead.
"""
import sys as _sys
import warnings as _warnings

# Re-export the old package's full public API — not just the `sidecar` submodule —
# so `from deepagent_vscode import main, run` and `deepagent_vscode.__version__`
# keep working through the transition window, exactly as before the rename.
# (gh #17; __version__ derives from installed metadata via langstage_vscode, per #9)
from langstage_vscode import __version__, main, run, sidecar  # noqa: F401

_warnings.warn(
    "deepagent_vscode has been renamed to langstage_vscode; "
    "this alias package will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

_sys.modules[__name__ + ".sidecar"] = sidecar

__all__ = ["main", "run", "sidecar", "__version__"]
