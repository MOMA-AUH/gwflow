"""Durable local termination evidence keyed by immutable Submission name."""

from hashlib import sha256
from pathlib import Path

from gwf.backends.local import LocalStatus

from . import _files


TERMINAL_STATES = frozenset((LocalStatus.COMPLETED, LocalStatus.FAILED,
                             LocalStatus.CANCELLED, LocalStatus.KILLED))
TERMINAL_NAMES = frozenset(state.name for state in TERMINAL_STATES)


class TerminationRecords:
    def __init__(self, working_dir):
        self.root = Path(working_dir) / ".gwf" / "local-termination"

    def path(self, submission):
        return self.root / (sha256(submission.encode()).hexdigest() + ".json")

    def read(self, submission):
        record = _files.read_json(self.path(submission))
        if (record and set(record) == {"submission", "state"}
                and record["submission"] == submission
                and isinstance(record["state"], str)
                and record["state"] in TERMINAL_NAMES):
            return LocalStatus[record["state"]]
        return LocalStatus.UNKNOWN

    def record(self, submission, state):
        # Publication is atomic and synced before exposing the corresponding
        # scheduler state. Missing or interrupted evidence never proves safety.
        _files.publish(self.path(submission), {"submission": submission, "state": state.name})
