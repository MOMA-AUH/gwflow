"""Deterministic backend observations through gwf's existing plugin contract."""

import json
from pathlib import Path

from gwf.backends import BackendStatus


class StateBackend:
    def __init__(self, working_dir):
        self.states = json.loads((Path(working_dir) / "backend-state.json").read_text())

    def status(self, target):
        if self.states.get(target.name) == "ERROR":
            raise RuntimeError(f"Backend query failed for {target.name}")
        return BackendStatus[self.states.get(target.name, "UNKNOWN")]

    def submit(self, target, dependencies):
        raise AssertionError("State fixture only supports non-submitting runs")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


setup = (StateBackend, -1)
