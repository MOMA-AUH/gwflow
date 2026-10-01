"""Shared-root witnesses and host-local device translation for owned storage.

Persistent device numbers are namespace labels from the initializing frontend.
An inode and an independently allocated witness directory identify each root on
other hosts. Syscall race checks continue to use actual local device numbers.
"""

import os
from pathlib import Path
import re
from uuid import uuid4

from gwf.exceptions import WorkflowError

from . import _files


def valid_witness(value):
    return (isinstance(value, dict) and set(value) == {"name", "inode"}
            and isinstance(value["name"], str)
            and re.fullmatch(r"\.gwflow-root-[0-9a-f]{32}", value["name"]) is not None
            and type(value["inode"]) is int and value["inode"] >= 0)


def allocate_witness(root):
    name = ".gwflow-root-" + uuid4().hex
    with _files.directory(root) as parent:
        os.mkdir(name, dir_fd=parent)
        _files.sync_directory(parent)
    return {"name": name, "inode": _files.identity(Path(root) / name)["inode"]}


class StorageIdentity:
    def __init__(self, locations, owner):
        self.devices = {}
        self.local_devices = {}
        if owner is None:
            for path in locations.values():
                if _files.exists(path):
                    _files.identity(path)
            return
        witnesses = owner.get("root_witnesses", {})
        if (not isinstance(witnesses, dict) or not witnesses.keys() <= locations.keys()
                or any(not valid_witness(value) for value in witnesses.values())):
            raise WorkflowError("Malformed managed root witness evidence")
        for key, path in locations.items():
            expected = owner["root_identity"][key]
            pending = owner.get(f"{key}_recreation") if key != "book" else None
            if pending is not None and "witness" in pending and not valid_witness(pending["witness"]):
                raise WorkflowError(f"Malformed {key}-root recreation witness")
            if not _files.exists(path):
                # Missing roots may be recreated only through the existing
                # activity/ownership protocol. Their surviving parent identifies
                # the local filesystem on which that recreation would happen.
                parent = path.parent
                while not _files.exists(parent):
                    parent = parent.parent
                found = _files.identity(parent)
                self._device(found["device"], expected["device"])
                continue
            found = _files.identity(path)
            if "root_witnesses" in owner and key not in witnesses and pending is None:
                raise WorkflowError(f"Missing managed root witness: {path}")
            candidates = [(expected, witnesses.get(key))]
            if pending is not None:
                candidates.append((pending["identity"], pending.get("witness")))
            for identity, witness in candidates:
                if found["inode"] != identity["inode"]:
                    continue
                if witness is None:
                    # Legacy records have no portable proof. Only the original
                    # device/inode check can authorize their frontend upgrade.
                    matches = found == identity
                else:
                    marker = Path(path) / witness["name"]
                    matches = (_files.exists(marker) and _files.identity(marker)
                               == {"device": found["device"], "inode": witness["inode"]})
                if matches:
                    self._device(found["device"], identity["device"])
                    break
            else:
                raise WorkflowError(f"Managed root identity changed: {path}")

    def _device(self, local, recorded):
        if (self.devices.get(local, recorded) != recorded
                or self.local_devices.get(recorded, local) != local):
            raise WorkflowError("Managed roots no longer share the recorded filesystems")
        self.devices[local] = recorded
        self.local_devices[recorded] = local

    def identity(self, path):
        found = _files.identity(path)
        if not self.devices:
            return found
        if found["device"] not in self.devices:
            raise WorkflowError(f"Managed directory is on an unowned filesystem: {path}")
        return {**found, "device": self.devices[found["device"]]}

    def local_identity(self, expected):
        try:
            return {**expected, "device": self.local_devices[expected["device"]]}
        except KeyError as error:
            raise WorkflowError("Managed directory names an unowned filesystem") from error

    def commit(self, source, destination, *, expected):
        _files.commit_directory(source, destination, expected=self.local_identity(expected))

    def remove(self, path, expected):
        _files.remove_directory(path, self.local_identity(expected))
