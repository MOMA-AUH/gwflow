"""Scheduled checked execution and complete-result transfer for managed Tasks."""

import os
from pathlib import Path
import subprocess
import sys

from gwf.exceptions import WorkflowError

from . import _files, admission, images
from .commands import Command
from .lifecycle import Store, target_dependencies
from .transfer import finish


def execute(store, attempt, local):
    store.validate_roots()
    store.check_inputs(attempt)
    store.require_current_execution(attempt, local)
    for dependency in target_dependencies(attempt["structure"], local):
        store.checked_target(attempt, dependency)
    home = store.execution_dir(attempt, local)
    staging, temporary = home / "staging", home / "tmp"
    if _files.exists(home):
        raise WorkflowError("Target execution storage already exists; unchecked work cannot be adopted")
    _files.mkdir(staging)
    _files.mkdir(temporary)
    execution = store.execution(attempt, local)
    declaration = execution["command"]
    if "literal" in declaration:
        command = declaration["literal"]
    else:
        command = Command(declaration["template"], declaration["bindings"]).render(
            lambda reference: (reference["external"] if "external" in reference
                               else store.retained_path(attempt, reference) if "task" in reference
                               else staging / reference["file"] if reference["target"] == local
                               else store.execution_dir(attempt, reference["target"]) / "committed" / reference["file"]))
    environment = os.environ.copy()
    if attempt["managed_tmpdir"]:
        environment["TMPDIR"] = str(temporary)
    image = attempt["images"].get(local)
    context = f"Task {attempt['task']} target {local}"
    arguments = ["/bin/bash", "-e", "-c", command]
    if image is not None:
        context += f" image {image['path']}"
        print(context, file=sys.stderr, flush=True)
        arguments = images.invocation(image, staging, temporary if attempt["managed_tmpdir"] else None,
                                      environment, command)
    try:
        result = subprocess.run(arguments, cwd=staging, env=environment)
    except OSError as error:
        raise WorkflowError(f"{context} launch failed: {error}") from error
    if result.returncode:
        raise WorkflowError(f"{context} command failed with exit {result.returncode}")
    store.validate_roots()
    paths = attempt["structure"]["targets"][local]["outputs"]
    observed = _files.metadata(staging, paths, sync=True)
    store.require_current_execution(attempt, local)
    _files.commit_directory(staging, home / "committed")
    store.publish(attempt, f"executions/{attempt['executions'][local]}/success.json", "target-success", target=local,
                  execution=attempt["executions"][local], outputs=observed, dependencies=execution["dependencies"])


def main():
    owner_path, task, attempt_id, operation, token, *args = sys.argv[1:]
    store = Store.for_job(owner_path)
    # Locate the selected record using the already recorded results location.
    current = _files.read_json(store._task_dir(task) / "current.json")
    if current is None or current.get("attempt") != attempt_id:
        raise WorkflowError("Scheduled job does not own the current Task attempt")
    attempt = store.read(current, "attempt.json", "attempt")
    if attempt is None or store.read(attempt, "ready.json", "ready") is None:
        raise WorkflowError("Task initialization is incomplete")
    attempt = store.current(task, attempt["result_dir"])
    local = args[0] if operation == "execute" else "gwflow_prepare" if operation == "prepare" else "gwflow_complete"
    intent = admission.read_intent(store, attempt, local)
    if intent is None or intent["admission"] != token:
        raise WorkflowError("Scheduled job does not own the selected submission generation")
    store.runtime_admission = token
    admission.require_dependencies(store, attempt, intent)
    try:
        if operation == "execute":
            execute(store, attempt, local)
        elif operation == "prepare":
            store.prepare(attempt)
        elif operation == "finish":
            finish(store, attempt)
        else:
            raise WorkflowError("Unknown managed job operation")
    except BaseException:
        store.publish(attempt, f"admissions/{token}/outcome.json", "job-outcome", **admission.identity(intent), state="failed")
        raise
    store.publish(attempt, f"admissions/{token}/outcome.json", "job-outcome", **admission.identity(intent), state="complete")


if __name__ == "__main__":
    try:
        main()
    except WorkflowError as error:
        print(f"gwflow: {error}", file=sys.stderr)
        raise SystemExit(1)
