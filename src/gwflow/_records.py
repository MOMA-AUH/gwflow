"""Logical attempt records over owned, individual metadata files.

Only this module maps attempt records and their history to storage paths.
Task selection and workflow ownership remain outside this boundary. Reads use
the existing bounded observation scope; instances never retain evidence or
directory handles of their own.
"""

import os

from . import _files
from .workflow import relative_path


class AttemptRecords:
    def __init__(self, task_directory, attempt):
        self._root = task_directory / "attempts" / attempt["attempt"]
        self._operation = attempt.get("operation")

    def _path(self, name):
        if name in ("staging.json", "transfer.json", "manifest.json", "installed.json",
                    "completion.json", "repair.json", "installation.json"):
            name = f"operations/{self._operation}/{name}"
        return self._root / relative_path(name)

    def read(self, name):
        return _files.read_json(self._path(name))

    def publish(self, name, record, *, replace=True):
        _files.publish(self._path(name), record, replace=replace)

    def exists(self, name):
        """Presence is independent of whether the evidence is readable or valid."""
        return _files.exists(self._path(name))

    def names(self, namespace="", *, initializing=False):
        """List immediate logical children, retaining unexpected names for validation."""
        root = self._path(namespace) if namespace else self._root
        if initializing:
            return sorted(_files.initialization_entries(root))
        if not _files.exists(root):
            return []
        with _files.directory(root) as directory:
            return sorted(os.listdir(directory))


def attempt_ids(task_directory):
    """Enumerate attempt identities without exposing their physical locations."""
    with _files.directory(task_directory / "attempts") as directory:
        return sorted(os.listdir(directory))
