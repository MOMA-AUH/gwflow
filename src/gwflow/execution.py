"""Scheduled checked execution and complete-result transfer for managed Tasks."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

from gwf.exceptions import WorkflowError

from . import _files
from .commands import Command
from .lifecycle import Store


def execute(store, attempt, local):
    store.validate_roots()
    store.check_inputs(attempt)
    home = store.execution_dir(attempt, local)
    staging, temporary = home / "staging", home / "tmp"
    if _files.exists(home):
        raise WorkflowError("Target execution storage already exists; unchecked work cannot be adopted")
    _files.mkdir(staging)
    _files.mkdir(temporary)
    declaration = attempt["commands"][local]
    if "literal" in declaration:
        command = declaration["literal"]
    else:
        command = Command(declaration["template"], declaration["bindings"]).render(
            lambda reference: reference["external"] if "external" in reference else staging / reference["file"])
    environment = os.environ.copy()
    if attempt["managed_tmpdir"]:
        environment["TMPDIR"] = str(temporary)
    result = subprocess.run(["/bin/bash", "-e", "-c", command], cwd=staging, env=environment)
    if result.returncode:
        raise WorkflowError(f"Task {attempt['task']} target {local} command failed with exit {result.returncode}")
    store.validate_roots()
    paths = attempt["structure"]["targets"][local]["outputs"]
    observed = _files.metadata(staging, paths, sync=True)
    _files.commit_directory(staging, home / "committed")
    store.publish(attempt, f"targets/{local}.json", "target-success", target=local,
                  execution=attempt["executions"][local], outputs=observed)


def finish(store, attempt):
    store.validate_roots()
    if any(_files.exists(store.attempt_dir(attempt) / filename)
           for filename in ("manifest.json", "installed.json", "completion.json")):
        raise WorkflowError("Existing transfer evidence requires recovery; it cannot be overwritten by another finishing job")
    if _files.exists(store.result_dir(attempt)):
        raise WorkflowError("Results destination already exists; existing results cannot be adopted or replaced")
    sources = {local: store.checked_target(attempt, local) for local in attempt["executions"]}
    staging = store.transfer_dir(attempt)
    if _files.exists(staging):
        raise WorkflowError("Interrupted transfer requires recovery; existing staging is not adopted")
    _files.mkdir(staging)
    retained = attempt["structure"]["retained"]
    for item in retained.values():
        local, source, destination = item["target"], item["source"], item["path"]
        root = store.execution_dir(attempt, local) / "committed"
        store.validate_roots()
        with _files.regular_file(root, source) as source_fd:
            with _files.regular_file(staging, destination, create=True) as destination_fd:
                with os.fdopen(os.dup(source_fd), "rb") as reader, os.fdopen(os.dup(destination_fd), "wb") as writer:
                    shutil.copyfileobj(reader, writer)
                    writer.flush()
                observed = sources[local]["outputs"][source]
                os.utime(destination_fd, ns=(observed["mtime_ns"], observed["mtime_ns"]))
                os.fsync(destination_fd)
        store.checked_target(attempt, local)
    copied = _files.metadata(staging, [item["path"] for item in retained.values()], sync=True)
    store.publish(attempt, "manifest.json", "manifest", operation=attempt["operation"],
                  sources={local: {"execution": record["execution"], "outputs": record["outputs"]}
                           for local, record in sources.items()},
                  retained=retained, outputs=copied, destination=str(store.result_dir(attempt)),
                  staged_identity=_files.identity(staging))
    store.validate_roots()
    if store.current(attempt["task"], attempt["result_dir"]) != attempt:
        raise WorkflowError("Current Task attempt changed before results commit")
    store.check_inputs(attempt)
    _files.commit_directory(staging, store.result_dir(attempt))
    destination_metadata = _files.metadata(store.result_dir(attempt), copied)
    store.publish(attempt, "installed.json", "installed", operation=attempt["operation"], outputs=destination_metadata)
    store.publish(attempt, "completion.json", "completion", operation=attempt["operation"], outputs=destination_metadata)


def main():
    owner_path, task, attempt_id, operation, *args = sys.argv[1:]
    store = Store.for_job(owner_path)
    # Locate the selected record using the already recorded results location.
    current = _files.read_json(store._task_dir(task) / "current.json")
    if current is None or current.get("attempt") != attempt_id:
        raise WorkflowError("Scheduled job does not own the current Task attempt")
    attempt = store.read(current, "attempt.json", "attempt")
    if attempt is None or store.read(attempt, "ready.json", "ready") is None:
        raise WorkflowError("Task initialization is incomplete")
    attempt = store.current(task, attempt["result_dir"])
    if operation == "execute":
        execute(store, attempt, args[0])
    elif operation == "prepare":
        store.prepare(attempt)
    elif operation == "finish":
        finish(store, attempt)
    else:
        raise WorkflowError("Unknown managed job operation")


if __name__ == "__main__":
    try:
        main()
    except WorkflowError as error:
        print(f"gwflow: {error}", file=sys.stderr)
        raise SystemExit(1)
