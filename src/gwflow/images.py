"""Check image dependencies and launch their authored commands."""

import csv
import io
import os

from gwf.exceptions import WorkflowError
from gwf.executors import Bash

from . import inputs


def check(structure, locations):
    for local, target in structure["targets"].items():
        if target["image"] is None:
            continue
        try:
            observed = inputs.observe([target["image"]], locations)
            if not os.access(observed[target["image"]]["resolved"], os.R_OK):
                raise WorkflowError("image is not readable")
        except WorkflowError as error:
            raise WorkflowError(f"Target {local}: image unavailable at {target['image']}: {error}") from error


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
    if temporary is None:
        temporary = environment.get("TMPDIR")
    if temporary is not None:
        arguments.extend(["--mount", mount(temporary)])
        environment["APPTAINERENV_TMPDIR"] = str(temporary)
    return [*arguments, image, "/bin/bash", "-e", "-c", command]
