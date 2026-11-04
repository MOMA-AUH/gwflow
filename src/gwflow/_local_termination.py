"""Durable local termination evidence keyed by immutable Submission name."""

from hashlib import sha256
from pathlib import Path

from gwf.backends.local import LocalStatus

from . import _files


class TerminationRecords:
    def __init__(self, working_dir):
        self.root = Path(working_dir) / ".gwf" / "local-termination"
        self.states = {}
        for path in self.root.glob("*.json"):
            record = _files.read_json(path)
            if not record or set(record) != {"submission", "state"}:
                continue
            name, state = record["submission"], record["state"]
            if (isinstance(name, str) and path == self.path(name)
                    and state in ("UNKNOWN", "COMPLETED", "FAILED", "CANCELLED", "KILLED")):
                self.states[name] = LocalStatus[state]

    def path(self, submission):
        return self.root / (sha256(submission.encode()).hexdigest() + ".json")

    def record(self, submission, state):
        # Publication is atomic and synced before exposing the corresponding
        # scheduler state. Missing or interrupted evidence never proves safety.
        _files.publish(self.path(submission), {"submission": submission, "state": state.name})
        self.states[submission] = state
