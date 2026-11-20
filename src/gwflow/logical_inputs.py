"""Task-scoped logical input declarations and their persisted physical selections."""

from collections.abc import Mapping
import json
import os
from pathlib import Path

from gwf.exceptions import WorkflowError
from gwf.utils import is_valid_name

from .inputs import declared_path


def _json_value(value, field):
    # JSON encoders silently coerce dictionary keys and tuples. Accept only
    # values that survive a strict JSON round trip without losing information.
    try:
        snapshot = json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
        if snapshot == value:
            return snapshot
    except (TypeError, ValueError, RecursionError):
        pass
    raise WorkflowError(f"Logical input {field} must be a JSON value with string object keys and finite numbers")


def _named_files(files):
    if not isinstance(files, Mapping) or not files:
        raise WorkflowError("Logical input files must be a nonempty mapping of names to external file paths")
    result = {}
    for key, path in files.items():
        if not isinstance(key, str) or not key:
            raise WorkflowError("Logical input files must have nonempty string names")
        try:
            path = os.fspath(path)
        except TypeError:
            path = None
        if not isinstance(path, str) or not path or "\0" in path:
            raise WorkflowError(f"Logical input files entry {key!r} must name an external file path")
        result[key] = path
    return result


class LogicalInput:
    """An opaque binding to one declaration in its owning Task."""

    def __init__(self, name, *, identity, files, metadata):
        if not isinstance(name, str) or not is_valid_name(name):
            raise WorkflowError(f"Invalid logical input name: {name!r}")
        self.name = name
        self._identity = _json_value(identity, "identity")
        self._metadata = _json_value(metadata, "metadata")
        self._files = _named_files(files)

    def selection(self, working_dir):
        return {"identity": self._identity,
                "representation": {"metadata": self._metadata,
                                   "files": {key: declared_path(path, working_dir) for key, path in self._files.items()}}}


def valid_selections(selections):
    if not isinstance(selections, dict):
        return False
    for name, selection in selections.items():
        if not isinstance(selection, dict) or set(selection) != {"identity", "representation"}:
            return False
        representation = selection["representation"]
        if not isinstance(representation, dict) or set(representation) != {"metadata", "files"}:
            return False
        # Reuse the declaration contract when accepting persisted data. Recorded
        # aliases are already absolute, including aliases that contain symlinks.
        try:
            LogicalInput(name, identity=selection["identity"], **representation)
        except WorkflowError:
            return False
        if not all(Path(path).is_absolute() for path in representation["files"].values()):
            return False
    return True


def selection_change(selected, frozen, *, compare_by_identity):
    # JSON distinguishes true, 1, and 1.0. Object member order is immaterial.
    def identities(selections):
        return json.dumps({name: value["identity"] for name, value in selections.items()}, sort_keys=True)

    if identities(selected) != identities(frozen):
        return "changed logical input name or identity"
    if not compare_by_identity and json.dumps(selected, sort_keys=True) != json.dumps(frozen, sort_keys=True):
        return "changed logical input representation; previous physical baseline cannot be used"
    return None


def descriptor(name, selection, baseline):
    representation = selection["representation"]
    return {"name": name, "identity": selection["identity"], "metadata": representation["metadata"],
            "files": {key: baseline[alias]["resolved"] for key, alias in representation["files"].items()}}
