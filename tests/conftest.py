"""Shared test fixtures.

``main()`` now ``chdir``s into the resolved workspace before running the agent
(ADR 0006), so an agent's raw relative file writes land in the workspace. That is
correct for a real one-shot sidecar process, but under pytest it would leak the cwd
into the next test. This autouse fixture snapshots and restores the process cwd
around every test so invocations stay isolated.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _restore_cwd():
    origin = os.getcwd()
    try:
        yield
    finally:
        os.chdir(origin)
