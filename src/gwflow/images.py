"""Resolve image dependencies on the frontend and launch local images on workers."""

import csv
import fcntl
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from gwf.exceptions import WorkflowError
from gwf.executors import Bash

from . import _observations, inputs


# Docker distribution reference grammar: repository, optional tag and digest.
# https://github.com/distribution/reference/blob/main/regexp.go
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_HOST_PART = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
_HOST = rf"(?:{_HOST_PART}(?:\.{_HOST_PART})*|\[[a-fA-F0-9:]+\])(?::[0-9]+)?"
_DIGEST = r"[A-Za-z][A-Za-z0-9]*(?:[-_+.][A-Za-z][A-Za-z0-9]*)*:[a-fA-F0-9]{32,}"
_REGISTRY_REFERENCE = re.compile(
    rf"docker://(?P<name>(?:{_HOST}/)?{_COMPONENT}(?:/{_COMPONENT})*)"
    rf"(?::[a-zA-Z0-9_][a-zA-Z0-9_.-]{{0,127}})?(?:@{_DIGEST})?"
)


def reference(value):
    try:
        value = os.fspath(value)
    except TypeError as error:
        raise WorkflowError("image must be a local SIF pathname or explicit docker:// reference") from error
    if not isinstance(value, str) or not value or "\0" in value:
        raise WorkflowError("image must be a local SIF pathname or explicit docker:// reference")
    if value.startswith("docker://"):
        match = _REGISTRY_REFERENCE.fullmatch(value)
        if match is None or len(match["name"]) > 255:
            raise WorkflowError(f"Malformed docker:// image reference: {value!r}")
        if "@" in value:
            algorithm, encoded = value.rsplit("@", 1)[1].split(":", 1)
            length = {"sha256": 64, "sha384": 96, "sha512": 128}.get(algorithm)
            if length is None:
                raise WorkflowError(f"Unsupported image digest algorithm: {algorithm!r}")
            if len(encoded) != length or encoded != encoded.lower():
                raise WorkflowError(f"Malformed {algorithm} image digest: {value!r}")
        return value
    if "://" in value or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:/", value):
        raise WorkflowError("image must be a local SIF pathname or explicit docker:// reference")
    return value


def binding(reference, working_dir):
    """Bind declarations to local paths without observing or acquiring images."""
    if not reference.startswith("docker://"):
        return inputs.declared_path(reference, working_dir)
    root = os.environ.get("GWFLOW_IMAGE_CACHE", "~/.cache/gwflow/images")
    if not root:
        raise WorkflowError("GWFLOW_IMAGE_CACHE must name an image cache directory")
    try:
        cache = (Path(working_dir) / Path(root).expanduser()).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise WorkflowError(f"Cannot resolve GWFLOW_IMAGE_CACHE {root!r}: {error}") from error
    key = hashlib.sha256(reference.encode()).hexdigest()
    return str(cache / (key + ".sif"))


def _readable(path, locations, *, refresh=False):
    observed = inputs.observe([str(path)], locations, refresh=refresh)
    if not os.access(observed[str(path)]["resolved"], os.R_OK):
        raise WorkflowError("image is not readable")


def _acquire(reference, path, locations):
    if os.path.lexists(path):
        _readable(path, locations)
        return
    inputs.validate_location(path, path.resolve(), locations)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the coordination file: unlinking it could let different callers
    # lock different inodes for the same entry. Closing the descriptor releases
    # ownership, including when the frontend is killed; workers do not inherit it.
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Waiting for image acquisition: {reference}", file=sys.stderr, flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        if not os.path.lexists(path):
            try:
                _pull(reference, path, locations)
            except (WorkflowError, OSError, RuntimeError) as error:
                print(f"Failed to pull image: {reference}: {error}", file=sys.stderr, flush=True)
                raise
        # This process or the lock's previous owner may have installed a new
        # image after an earlier successful observation of this same alias.
        _readable(path, locations, refresh=True)


def _pull(reference, path, locations):
    with tempfile.TemporaryDirectory(prefix=".pull-", dir=path.parent) as temporary:
        acquired = Path(temporary) / "image.sif"
        print(f"Pulling image: {reference}", file=sys.stderr, flush=True)
        result = subprocess.run(["apptainer", "pull", "--disable-cache", str(acquired), reference],
                                capture_output=True, text=True)
        if result.returncode:
            raise WorkflowError(f"Apptainer pull failed (exit {result.returncode}): {result.stderr.strip()}")
        _readable(acquired, locations)
        try:
            os.link(acquired, path)
        except FileExistsError:
            # Never overwrite an entry created outside acquisition coordination.
            pass
    print(f"Pulled image: {reference}", file=sys.stderr, flush=True)


def resolve(task, structure, locations):
    for local, target in structure["targets"].items():
        if target["image"] is None:
            continue
        reference = task.targets[local].image
        try:
            def inspect():
                if reference.startswith("docker://"):
                    _acquire(reference, Path(target["image"]), locations)
                else:
                    _readable(target["image"], locations)
            _observations.reuse("image", (reference, target["image"], tuple(sorted(locations.items()))), inspect)
        except (WorkflowError, OSError, RuntimeError) as error:
            raise WorkflowError(f"Target {local}: image unavailable for {reference!r} at {target['image']}: {error}") from error


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
