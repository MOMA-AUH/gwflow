"""Declared container input layouts and symlinks to checked source files."""

import os
from pathlib import Path

from . import _files
from .workflow import relative_path, validate_destinations


def external_layout(references, outputs):
    inputs = [value for value in references if isinstance(value, str)]
    names = [relative_path(Path(value).name) for value in inputs]
    validate_destinations([*names, *outputs])
    return dict(zip(names, inputs))


def stage_external(layout, baseline, work):
    paths, sources = {}, set()
    for name, reference in layout.items():
        source = Path(baseline[reference]["resolved"])
        destination = work / name
        with _files.directory(destination.parent, create=True) as parent:
            os.symlink(source, destination.name, dir_fd=parent)
            _files.sync_directory(parent)
        paths[reference] = destination
        sources.add(source.parent)
    return paths, sorted(sources, key=lambda path: (len(path.parts), str(path)))
