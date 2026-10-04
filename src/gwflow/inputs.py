"""External paths keep their declared aliases and compare resolved metadata."""

import os
from pathlib import Path
import stat

from gwf.exceptions import WorkflowError


def declared_path(value, working_dir):
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise WorkflowError("External inputs must be declared file paths")
    return str((Path(working_dir) / value).absolute())


def validate_location(path, resolved, locations):
    for candidate in (path, resolved):
        if any(candidate == root or root in candidate.parents for root in locations.values()):
            raise WorkflowError(f"External input points into managed storage: {path}")


def observe(paths, locations):
    result = {}
    for alias in paths:
        try:
            path = Path(alias)
            resolved = path.resolve(strict=True)
            validate_location(path, resolved, locations)
            info = resolved.stat()
            if not stat.S_ISREG(info.st_mode):
                raise WorkflowError(f"External input must resolve to a regular file: {alias}")
            result[alias] = {"resolved": str(resolved), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
        except (OSError, RuntimeError) as error:
            raise WorkflowError(f"Cannot observe external input {alias}: {error}") from error
    return result


def valid(observations, paths):
    return (isinstance(observations, dict) and observations.keys() == set(paths)
            and all(isinstance(value, dict) and isinstance(value.get("resolved"), str)
                    and Path(value["resolved"]).is_absolute()
                    and type(value.get("size")) is int and value["size"] >= 0
                    and type(value.get("mtime_ns")) is int for value in observations.values()))
