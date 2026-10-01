"""Declared container input layouts and symlinks to checked source files."""

import json
import os
from pathlib import Path

from gwf.exceptions import WorkflowError

from . import _files
from .workflow import relative_path, validate_destinations


def reference_key(reference):
    return json.dumps(reference, sort_keys=True)


def input_layout(references, outputs, retained_path, overrides):
    assigned = {}
    for name, reference in overrides.items():
        if reference not in references:
            raise WorkflowError("stage_as must reference an already-declared target input")
        key = reference_key(reference)
        if key in assigned:
            raise WorkflowError("stage_as assigns multiple paths to one logical input")
        assigned[key] = relative_path(name)
    names = [assigned.get(reference_key(value)) or
             relative_path(Path(value if isinstance(value, str) else
                                value["file"] if "target" in value else retained_path(value)).name)
             for value in references]
    validate_destinations([*names, *outputs])
    return dict(zip(names, references))


def valid_layout(layout, references, outputs):
    if (not isinstance(layout, dict) or any(relative_path(name) != name for name in layout)
            or sorted(map(reference_key, layout.values())) != sorted(map(reference_key, references))):
        return False
    validate_destinations([*layout, *outputs])
    return True


def stage(layout, work, resolve_source):
    paths, sources = {}, set()
    for name, reference in layout.items():
        source = Path(resolve_source(reference))
        destination = work / name
        with _files.directory(destination.parent, create=True) as parent:
            os.symlink(source, destination.name, dir_fd=parent)
            _files.sync_directory(parent)
        paths[reference_key(reference)] = destination
        sources.add(source.parent)
    return paths, sorted(sources, key=lambda path: (len(path.parts), str(path)))
