"""Declared container input layouts and symlinks to checked source files."""

import json
import os
from pathlib import Path

from gwf.exceptions import WorkflowError

from . import _files, logical_inputs
from .workflow import relative_path, validate_destinations


def reference_key(reference):
    return json.dumps(reference, sort_keys=True)


def input_layout(references, outputs, retained_path, overrides):
    def default_name(value):
        if isinstance(value, str):
            return Path(value).name
        if "logical_input" in value:
            return value["logical_input"] + ".json"
        path = value["file"] if "target" in value else retained_path(value)
        return Path(path).name

    assigned = {}
    for name, reference in overrides.items():
        if reference not in references:
            raise WorkflowError("stage_as must reference an already-declared target input")
        key = reference_key(reference)
        if key in assigned:
            raise WorkflowError("stage_as assigns multiple paths to one logical input")
        assigned[key] = relative_path(name)
    names = [assigned.get(reference_key(value)) or relative_path(default_name(value))
             for value in references]
    try:
        validate_destinations([*names, *outputs])
    except WorkflowError as error:
        raise WorkflowError(f"{error}. Use stage_as to choose distinct input paths.") from error
    return dict(zip(names, references))


def valid_layout(layout, references, outputs):
    if (not isinstance(layout, dict) or any(relative_path(name) != name for name in layout)
            or sorted(map(reference_key, layout.values())) != sorted(map(reference_key, references))):
        return False
    validate_destinations([*layout, *outputs])
    return True


def _link(source, destination):
    with _files.directory(destination.parent, create=True) as parent:
        os.symlink(source, destination.name, dir_fd=parent)
        _files.sync_directory(parent)


def prepare_descriptors(target, selections, baseline, directory):
    paths, sources = {}, set()
    for reference in target["inputs"]:
        if not isinstance(reference, dict) or "logical_input" not in reference:
            continue
        name = reference["logical_input"]
        path = directory / f"{len(paths)}.json"
        document = logical_inputs.descriptor(name, selections[name], baseline)
        if target["image"] is not None:
            # File keys are arbitrary JSON strings. Keep them in the descriptor
            # and allocate private filenames independently of keys and basenames.
            for index, key in enumerate(sorted(document["files"])):
                source = Path(document["files"][key])
                destination = path.with_suffix("") / str(index)
                _link(source, destination)
                document["files"][key] = str(destination)
                sources.add(source.parent)
        _files.publish(path, document, replace=False)
        paths[name] = path
    return paths, sources


def stage(layout, work, resolve_source):
    paths, sources = {}, set()
    for name, reference in layout.items():
        source = Path(resolve_source(reference))
        destination = work / name
        _link(source, destination)
        paths[reference_key(reference)] = destination
        sources.add(source.parent)
    return paths, sources
