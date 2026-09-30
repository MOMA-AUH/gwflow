"""Observe and finish owned retained-result transfers."""

import errno
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from uuid import uuid4

from gwf.exceptions import WorkflowError

from . import _files
from .lifecycle import _valid_metadata


@dataclass
class Recovery:
    action: str
    reason: str
    ownership: dict | None
    manifest: dict | None = None
    removal: dict | None = None


def _manifest(store, attempt, sources, record):
    manifest = store.read(attempt, "manifest.json", "manifest", operation=attempt["operation"])
    retained = attempt["structure"]["retained"]
    if (manifest is None or record is None
            or manifest.get("sources") != sources or manifest.get("retained") != retained
            or any(manifest.get(key) != record[key] for key in ("copy", "staging", "destination", "staged_identity"))
            or not _valid_metadata(manifest.get("outputs"), [item["path"] for item in retained.values()])
            or any(manifest["outputs"][item["path"]]["size"] != sources[item["target"]]["outputs"][item["source"]]["size"]
                   for item in retained.values())):
        raise WorkflowError("Missing or malformed prepared transfer manifest")
    return manifest


def _checked_set(root, manifest):
    return (_files.identity(root) == manifest["staged_identity"]
            and _files.file_set(root) == set(manifest["outputs"])
            and _files.metadata(root, manifest["outputs"]) == manifest["outputs"])


class InvalidSources(WorkflowError):
    """Transfer cannot recover without another computation execution."""


def _sources(store, attempt, *, check_files):
    sources = {}
    for local in attempt["executions"]:
        try:
            record = store.checked_target(attempt, local, check_files=check_files)
        except (WorkflowError, OSError) as error:
            raise InvalidSources(f"Invalid transfer source for {local!r}: {error}") from error
        sources[local] = {"execution": record["execution"], "outputs": record["outputs"]}
    return sources


def inspect(store, attempt):
    """Check recovery eligibility without changing files or allocating identities."""
    store.validate_roots()
    store.check_inputs(attempt)
    sources = _sources(store, attempt, check_files=False)
    record = store.transfer_ownership(attempt)
    destination = store.result_dir(attempt)
    installed = _files.exists(destination)
    if installed and (record is None or _files.identity(destination) != record["staged_identity"]):
        raise WorkflowError("Cannot establish ownership of installed transfer results")
    if record is not None and _files.exists(Path(record["staging"])):
        if _files.identity(Path(record["staging"])) != record["staged_identity"]:
            raise WorkflowError("Transfer staging ownership changed")
    try:
        manifest = _manifest(store, attempt, sources, record)
        root = destination if installed else Path(manifest["staging"])
        if _checked_set(root, manifest):
            if installed:
                return Recovery("complete", "finish checked installed retained results under the same attempt", record, manifest)
            return Recovery("install", "install prepared retained-result set under the same attempt", record, manifest)
    except (WorkflowError, OSError):
        # Strong ownership was checked above. Incomplete association or copied
        # data can only be rebuilt from separately verified source work.
        pass
    _sources(store, attempt, check_files=True)
    return Recovery("copy", "rebuild owned retained results from checked work under the same attempt" if installed else
                    "restart retained-result copying from checked work under the same attempt", record,
                    removal=record["staged_identity"] if installed else None)


def _prepare(store, attempt, previous):
    sources = _sources(store, attempt, check_files=True)
    if previous is not None and _files.exists(Path(previous["staging"])):
        _files.remove_directory(Path(previous["staging"]), previous["staged_identity"])
    copy = uuid4().hex
    staging = store.transfer_dir(attempt) / copy
    _files.mkdir(staging)
    store.publish(attempt, "transfer.json", "transfer", operation=attempt["operation"], copy=copy,
                  staging=str(staging), destination=str(store.result_dir(attempt)), staged_identity=_files.identity(staging))
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
                try:
                    os.utime(destination_fd, ns=(observed["mtime_ns"], observed["mtime_ns"]))
                except OSError as error:
                    if error.errno not in (errno.ENOTSUP, errno.ENOSYS):
                        raise
                os.fsync(destination_fd)
        store.checked_target(attempt, local)
    copied = _files.metadata(staging, [item["path"] for item in retained.values()], sync=True)
    store.publish(attempt, "manifest.json", "manifest", operation=attempt["operation"],
                  sources=sources, retained=retained, outputs=copied, destination=str(store.result_dir(attempt)),
                  copy=copy, staging=str(staging),
                  staged_identity=_files.identity(staging))
    return _manifest(store, attempt, sources, store.transfer_ownership(attempt))


def finish(store, attempt):
    recovery = inspect(store, attempt)
    if recovery.removal is not None:
        _files.remove_directory(store.result_dir(attempt), recovery.removal)
    manifest = recovery.manifest if recovery.action in ("install", "complete") else _prepare(store, attempt, recovery.ownership)
    store.validate_roots()
    if store.current(attempt["task"], attempt["result_dir"]) != attempt:
        raise WorkflowError("Current Task attempt changed before results commit")
    store.check_inputs(attempt)
    if recovery.action == "complete":
        if not _checked_set(store.result_dir(attempt), manifest):
            raise WorkflowError("Installed transfer changed before Completion")
    else:
        staging = Path(manifest["staging"])
        if not _checked_set(staging, manifest):
            raise WorkflowError("Prepared transfer changed before installation")
        _files.commit_directory(staging, store.result_dir(attempt), expected=manifest["staged_identity"])
    destination_metadata = _files.metadata(store.result_dir(attempt), manifest["outputs"])
    store.publish(attempt, "installed.json", "installed", operation=attempt["operation"], outputs=destination_metadata)
    store.publish(attempt, "completion.json", "completion", operation=attempt["operation"], outputs=destination_metadata,
                  producers=attempt["producers"])
