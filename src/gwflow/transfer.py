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
            or any(manifest.get(key) != record[key] for key in ("copy", "staging", "destination", "staged_identity", "replaces"))
            or not _valid_metadata(manifest.get("outputs"), [item["path"] for item in retained.values()])
            or any(manifest["outputs"][item["path"]]["size"] != sources[item["target"]]["outputs"][item["source"]]["size"]
                   for item in retained.values())):
        raise WorkflowError("Missing or malformed prepared transfer manifest")
    return manifest


def _checked_set(root, manifest):
    return (_files.same_directory(_files.identity(root), manifest["staged_identity"])
            and _files.file_set(root) == set(manifest["outputs"])
            and _files.metadata(root, manifest["outputs"]) == manifest["outputs"])


class InvalidSources(WorkflowError):
    """Transfer cannot recover without another computation execution."""


def _check_source(store, attempt, sources, local, filename):
    root = store.execution_dir(attempt, local) / "committed"
    if _files.metadata(root, [filename])[filename] != sources[local]["outputs"][filename]:
        raise WorkflowError(f"Retained transfer source changed: {local}/{filename}")


def _sources(store, attempt, *, check_files):
    try:
        sources = store.source_evidence(attempt)
        if check_files:
            for item in attempt["structure"]["retained"].values():
                _check_source(store, attempt, sources, item["target"], item["source"])
        return sources
    except (WorkflowError, OSError) as error:
        raise InvalidSources(f"Invalid transfer source: {error}") from error


@dataclass
class RepairObservation:
    sources: dict
    removal: dict | None


def inspect_repair(store, attempt):
    """Establish repair eligibility before invalidating the previous Completion."""
    store.validate_roots()
    if store.cleanup_record(attempt) is not None:
        raise InvalidSources("Work is marked for cleanup; repair requires fresh computation")
    sources = _sources(store, attempt, check_files=True)
    requires_logical = bool(attempt["structure"].get("logical_inputs")) and store.completion_manifest(attempt) is None
    store.check_inputs(attempt, include_logical=requires_logical)
    removal = store.result_removal(attempt)
    return RepairObservation(sources, removal)


def _installation(store, attempt, manifest):
    record = store.read(attempt, "installation.json", "installation", operation=attempt["operation"])
    if (record is None or any(record.get(key) != manifest[key] for key in ("copy", "destination", "staged_identity"))
            or "removal" not in record or record["removal"] is not None and not _files.valid_identity(record["removal"])):
        raise WorkflowError("Missing or malformed repair installation intent")
    return record


def _checked_manifest(store, attempt, sources, record, *, installed):
    try:
        manifest = _manifest(store, attempt, sources, record)
        root = store.result_dir(attempt) if installed else Path(manifest["staging"])
        if _checked_set(root, manifest):
            if installed and store.repair_intent(attempt) is not None:
                _installation(store, attempt, manifest)
            return manifest
    except (WorkflowError, OSError):
        # A missing or damaged set may still be rebuilt from checked work.
        pass
    return None


def has_checked_installation(store, attempt):
    """The owned, installed set proves the pre-installation input check already passed."""
    if not store.exists(attempt, "transfer.json"):
        return False
    try:
        record = store.transfer_ownership(attempt)
        sources = _sources(store, attempt, check_files=False)
        return _checked_manifest(store, attempt, sources, record, installed=True) is not None
    except (WorkflowError, OSError):
        return False


def _check_transfer_inputs(store, attempt, *, repair, checked_installation):
    store.check_inputs(attempt, include_logical=repair is None and not checked_installation)


def inspect(store, attempt):
    """Check recovery eligibility without changing files or allocating identities."""
    store.validate_roots()
    repair = store.repair_intent(attempt)
    sources = _sources(store, attempt, check_files=False)
    record = store.transfer_ownership(attempt)
    staging = store.staging_record(attempt)
    if (staging is not None and _files.exists(staging["destination"])
            and not _files.same_directory(_files.identity(staging["destination"]), staging["identity"])):
        raise WorkflowError("Transfer staging directory ownership changed")
    if repair is not None and repair["sources"] != sources:
        raise InvalidSources("Repair source execution evidence changed")
    destination = store.result_dir(attempt)
    identity = _files.identity(destination) if _files.exists(destination) else None
    installed = record is not None and _files.same_directory(identity, record["staged_identity"])
    old = (repair is not None and _files.same_directory(identity, repair["result_identity"])
           or record is not None and _files.same_directory(identity, record["replaces"]))
    if identity is not None and not (installed or old):
        raise WorkflowError("Cannot establish ownership of installed transfer results")
    if record is not None and _files.exists(Path(record["staging"])):
        if not _files.same_directory(_files.identity(Path(record["staging"])), record["staged_identity"]):
            raise WorkflowError("Transfer staging ownership changed")
    manifest = _checked_manifest(store, attempt, sources, record, installed=installed)
    _check_transfer_inputs(store, attempt, repair=repair, checked_installation=installed and manifest is not None)
    if manifest is not None:
        if installed:
            return Recovery("complete", "finish checked installed retained results under the same attempt", record, manifest)
        return Recovery("install", "install prepared retained-result set under the same attempt", record, manifest,
                        removal=identity)
    _sources(store, attempt, check_files=True)
    return Recovery("copy", "rebuild owned retained results from checked work under the same attempt" if installed else
                    "restart retained-result copying from checked work under the same attempt", record,
                    removal=identity)


def _prepare(store, attempt, previous, removal):
    sources = _sources(store, attempt, check_files=True)
    directory = store.ensure_staging(attempt)
    if previous is not None and _files.exists(Path(previous["staging"])):
        _files.remove_directory(Path(previous["staging"]), previous["staged_identity"])
    copy = uuid4().hex
    staging = directory / copy
    _files.mkdir(staging)
    store.publish(attempt, "transfer.json", "transfer", operation=attempt["operation"], copy=copy,
                  staging=str(staging), destination=str(store.result_dir(attempt)), staged_identity=_files.identity(staging),
                  replaces=removal)
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
        _check_source(store, attempt, sources, local, source)
    copied = _files.metadata(staging, [item["path"] for item in retained.values()], sync=True)
    store.publish(attempt, "manifest.json", "manifest", operation=attempt["operation"],
                  sources=sources, retained=retained, outputs=copied, destination=str(store.result_dir(attempt)),
                  copy=copy, staging=str(staging),
                  staged_identity=_files.identity(staging), replaces=removal)
    return _manifest(store, attempt, sources, store.transfer_ownership(attempt))


def finish(store, attempt):
    recovery = inspect(store, attempt)
    manifest = (recovery.manifest if recovery.action in ("install", "complete")
                else _prepare(store, attempt, recovery.ownership, recovery.removal))
    store.validate_roots()
    if store.current(attempt["task"], attempt["result_dir"]) != attempt:
        raise WorkflowError("Current Task attempt changed before results commit")
    store.validate_result_ownership({attempt["task"]: attempt["result_dir"]})
    repair = store.repair_intent(attempt)
    _check_transfer_inputs(store, attempt, repair=repair, checked_installation=recovery.action == "complete")
    if recovery.action == "complete":
        if not _checked_set(store.result_dir(attempt), manifest):
            raise WorkflowError("Installed transfer changed before Completion")
    else:
        staging = Path(manifest["staging"])
        if not _checked_set(staging, manifest):
            raise WorkflowError("Prepared transfer changed before installation")
        if repair is not None:
            store.publish(attempt, "installation.json", "installation", operation=attempt["operation"],
                          copy=manifest["copy"], staged_identity=manifest["staged_identity"],
                          destination=manifest["destination"], removal=recovery.removal)
        if recovery.removal is not None:
            _files.remove_directory(store.result_dir(attempt), recovery.removal)
        _files.commit_directory(staging, store.result_dir(attempt), expected=manifest["staged_identity"])
    destination_metadata = _files.metadata(store.result_dir(attempt), manifest["outputs"])
    store.publish(attempt, "installed.json", "installed", operation=attempt["operation"], outputs=destination_metadata)
    store.publish(attempt, "completion.json", "completion", operation=attempt["operation"], outputs=destination_metadata,
                  producers=attempt["producers"])
