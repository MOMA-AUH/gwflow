"""Private, versioned completion bookkeeping written by ordinary gwf jobs."""

import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from uuid import uuid4

from gwf import Target


SCHEMA = 1


def read_record(path):
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(record, dict):
        return None
    if type(record.get("schema")) is not int or record["schema"] != SCHEMA:
        return None
    attempt = record.get("attempt")
    if not isinstance(attempt, str) or len(attempt) != 32:
        return None
    if any(char not in "0123456789abcdef" for char in attempt):
        return None
    if not isinstance(record.get("task"), str):
        return None
    if not isinstance(record.get("definition"), dict):
        return None
    return record


def atomic_write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Completion:
    """One task's expected attempt, independent of generated target inputs."""

    def __init__(self, working_dir, name, definition, commands, options):
        self.directory = Path(working_dir) / ".gwf" / "gwflow" / name
        self.expected_path = self.directory / "expected.json"
        self.name = name
        self.definition = definition
        self.commands = commands
        self.options = options
        self.record = read_record(self.expected_path)
        self.replaced = False

    @property
    def path(self):
        return self.directory / (self.record["attempt"] + ".json")

    def matches_definition(self):
        return (
            self.record is not None
            and self.record["task"] == self.name
            and self.record["definition"] == self.definition
            and (
                self.commands is None
                or self.record.get("commands") == self.commands
            )
        )

    def is_complete(self):
        return self.matches_definition() and read_record(self.path) == self.record

    def prepare(self, *, new_work=False):
        # A pending missing record can be recovered by the same finalizer. An
        # existing invalid record needs an absent output even without hashes.
        if not self.replaced and (
            new_work
            or not self.matches_definition()
            or (self.path.exists() and not self.is_complete())
        ):
            self.record = {
                "schema": SCHEMA,
                "task": self.name,
                "attempt": uuid4().hex,
                "definition": self.definition,
            }
            if self.commands is not None:
                self.record["commands"] = self.commands
            self.replaced = True

    def persist(self):
        if self.replaced:
            atomic_write(self.expected_path, self.record)

    def target(self, targets, working_dir):
        command = shlex.join([
            sys.executable, "-m", "gwflow.completion", str(self.path),
            json.dumps(self.record, sort_keys=True),
        ])
        return Target(
            name=f"{self.name}__gwflow_complete",
            inputs=sorted({
                path for target in targets for path in target.flattened_outputs()
            }),
            outputs=[str(self.path)],
            options=self.options.copy(),
            working_dir=working_dir,
            spec=command,
        )


if __name__ == "__main__":
    atomic_write(Path(sys.argv[1]), json.loads(sys.argv[2]))
