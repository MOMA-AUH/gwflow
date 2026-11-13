"""Durable local termination evidence keyed by immutable Submission name."""

from contextlib import contextmanager, nullcontext
import fcntl
import os
from pathlib import Path
import re
import stat

from gwf.backends.local import LocalStatus
from gwf.exceptions import WorkflowError

from . import _files


TERMINAL_STATES = frozenset((LocalStatus.COMPLETED, LocalStatus.FAILED,
                             LocalStatus.CANCELLED, LocalStatus.KILLED))
TERMINAL_NAMES = frozenset(state.name for state in TERMINAL_STATES)
STATE_NAMES = frozenset(state.name for state in LocalStatus)


class TerminationRecords:
    def __init__(self, working_dir):
        self.root = Path(working_dir) / ".gwf" / "local-termination"
        self.path = self.root / "records.json"

    @staticmethod
    def _valid(record):
        return (isinstance(record, dict) and set(record) == {"version", "submissions"}
                and type(record["version"]) is int and record["version"] == 1
                and isinstance(record["submissions"], dict)
                and all(isinstance(state, str) and state in STATE_NAMES
                        for state in record["submissions"].values()))

    def read(self, submission):
        # Atomic replacement gives readers a complete snapshot without taking
        # a workflow lock while they may already hold an attempt record lock.
        state = _files.read_json(
            self.path, validate=self._valid,
            select=lambda record: record["submissions"].get(submission) if record is not None else None)
        if state in TERMINAL_NAMES:
            return LocalStatus[state]
        return LocalStatus.UNKNOWN

    def record(self, submission, state):
        # Writers never acquire attempt locks or wait for a job. The completion
        # job's own late receipt therefore uses the same bounded storage after
        # compaction, including when another scheduler publishes concurrently.
        with self._writer() as directory:
            self._publish(directory, submission, state)

    def resume(self):
        """Discard interrupted publications without changing authoritative history."""
        with self._writer():
            pass

    @contextmanager
    def _writer(self):
        with _files.directory(self.root, create=True) as directory:
            lock = os.open("records.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=directory)
            try:
                info = os.fstat(lock)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkflowError("Local termination lock must be a regular unlinked file")
                fcntl.flock(lock, fcntl.LOCK_EX)
                current = os.stat("records.lock", dir_fd=directory, follow_symlinks=False)
                if _files._signature(current) != _files._signature(info):
                    raise WorkflowError("Local termination lock changed during acquisition")
                self._discard_pending(directory)
                yield directory
            finally:
                os.close(lock)

    def _publish(self, directory, submission, state):
        record = _files.read_json(self.path, parent_directory=nullcontext(directory))
        if record is None:
            try:
                os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                record = {"version": 1, "submissions": {}}
        if not self._valid(record):
            raise WorkflowError("Malformed local termination evidence; refusing to discard submission history")
        record["submissions"][submission] = state.name
        _files.publish(self.path, record, parent_directory=nullcontext(directory))

    def _discard_pending(self, directory):
        # A killed publisher can leave a private temporary file. Under the sole
        # writer lock these files are abandoned, never authoritative evidence.
        for name in os.listdir(directory):
            if re.fullmatch(r"\.pending-[0-9a-f]{32}", name):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkflowError("Invalid interrupted local termination publication")
                os.unlink(name, dir_fd=directory)
        _files.sync_directory(directory)
