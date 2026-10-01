"""Observe local images at the frontend and launch their authored commands."""

import csv
import io
import os
from pathlib import Path
import stat

from gwf.exceptions import WorkflowError
from gwf.executors import Bash


def observe(task, working_dir):
    observed = {}
    for local, target in task.targets.items():
        if target.image is None:
            continue
        path = Path(working_dir) / target.image
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
            if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.R_OK):
                raise OSError("image is not a readable regular file")
        except (OSError, RuntimeError) as error:
            raise WorkflowError(f"Target {local}: image unavailable at {path}: {error}") from error
        observed[local] = {"path": str(resolved), "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns}
    return observed


def validate_executors(task, workflow):
    selected = {local: target for local, target in task.targets.items() if target.image is not None}
    if not selected:
        return
    boundaries = [(local, target.executor or workflow.executor, target.options)
                  for local, target in selected.items()]
    boundaries.extend((local, workflow.executor, {**workflow.defaults, **defaults})
                      for local, defaults in (("gwflow_prepare", workflow.preparation_defaults),
                                              ("gwflow_complete", workflow.completion_defaults)))
    for local, executor, options in boundaries:
        if any(value is not None and type(value) is not Bash for value in (executor, options.get("executor"))):
            raise WorkflowError(f"Container Task {local} requires the ordinary gwf Bash executor")


def valid(observations, targets):
    return (isinstance(observations, dict) and observations.keys() <= targets.keys()
            and all(isinstance(value, dict) and set(value) == {"path", "size", "mtime_ns"}
                    and isinstance(value["path"], str) and Path(value["path"]).is_absolute()
                    and type(value["size"]) is int and value["size"] >= 0
                    and type(value["mtime_ns"]) is int for value in observations.values()))


def mount(path, *, readonly=False):
    value = io.StringIO()
    fields = ["type=bind", f"src={path}", f"dst={path}"]
    if readonly:
        fields.append("ro")
    csv.writer(value, lineterminator="").writerow(fields)
    return value.getvalue()


def invocation(image, work, temporary, environment, command, source_directories=()):
    arguments = ["apptainer", "exec", "--cleanenv", "--no-eval", "--pwd", str(work)]
    for source in source_directories:
        arguments.extend(["--mount", mount(source, readonly=True)])
    arguments.extend(["--mount", mount(work)])
    if temporary is not None:
        arguments.extend(["--mount", mount(temporary)])
        environment["APPTAINERENV_TMPDIR"] = str(temporary)
    return [*arguments, image["path"], "/bin/bash", "-e", "-c", command]
