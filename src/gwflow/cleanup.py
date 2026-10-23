"""Observe and durably remove eligible owned disposable Task storage."""

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from gwf.exceptions import WorkflowError

from . import _files, admission


@dataclass
class CleanupObservation:
    attempt: dict
    action: str
    reason: str
    directories: dict = field(default_factory=dict)
    explicit: bool = False
    paths_error: str = ""
    removed_paths: list[str] = field(default_factory=list)
    removing_path: str | None = None

    @property
    def outcome(self):
        if self.action == "removed" and not self.removed_paths:
            return "already_removed"
        if self.action == "keep" and self.removed_paths:
            return "partial"
        return self.action


def observe(store, attempt, backend, backend_name, *, explicit=False):
    observation = CleanupObservation(attempt, "keep", "incomplete computation or results", explicit=explicit)
    try:
        store.validate_roots()
        if not explicit and store.recorded_current(attempt["task"])["attempt"] != attempt["attempt"]:
            observation.reason = "superseded attempt; default cleanup keeps older work"
            return observation
        jobs = admission.activity(store, attempt, backend, backend_name)
        uncertain = [job.submission for job in jobs if job.state == "uncertain"]
        active = [job.submission for job in jobs if job.state == "active"]
        if uncertain:
            observation.action, observation.reason = "blocked", "unresolved submission: " + ", ".join(uncertain)
            return observation
        if active:
            if explicit:
                observation.action = "blocked"
            observation.reason = "active work: " + ", ".join(active)
            return observation
        if not explicit:
            try:
                completed = store.completed(attempt)
            except (WorkflowError, OSError):
                completed = False
            if not completed:
                observation.reason = "Completion or retained results are invalid; keep work for retry or repair"
                return observation
        record = store.cleanup_record(attempt)
        workspace = store.workspace(attempt)
        identity = store.workspace_identity(attempt)
        observation.directories[str(workspace)] = identity
        observation.directories.update(store.staging_directories(attempt))
        for path, identity in observation.directories.items():
            if _files.exists(path) and not _files.same_directory(_files.identity(path), identity):
                raise WorkflowError(f"Disposable directory ownership changed: {path}")
        if record is not None and record["directories"] != observation.directories:
            raise WorkflowError("Cleanup ownership evidence changed")
        if record is not None and any(_files.exists(path) for path in record.get("removed_paths", [])):
            raise WorkflowError("Removed work was recreated; existing files are not adopted")
        if record is not None and record["state"] == "removed":
            if any(_files.exists(path) for path in observation.directories):
                raise WorkflowError("Removed work was recreated; existing files are not adopted")
            observation.action, observation.reason = "removed", "work already removed"
        else:
            observation.action = "eligible"
            observation.reason = ("explicitly selected inactive attempt" if explicit else
                                  "checked Completion and retained results; no active work")
    except (WorkflowError, OSError) as error:
        observation.action, observation.reason = "blocked", str(error)
    finally:
        if observation.action in ("keep", "blocked"):
            try:
                observation.directories.update(store.staging_directories(attempt))
            except (WorkflowError, OSError) as error:
                observation.paths_error = str(error)
    return observation


def remove(store, observation, backend, backend_name):
    """Keep invocation receipts visible even when removal cannot finish."""
    try:
        checked = observe(store, observation.attempt, backend, backend_name, explicit=observation.explicit)
        observation.action, observation.reason = checked.action, checked.reason
        observation.directories, observation.paths_error = checked.directories, checked.paths_error
        if observation.action in ("eligible", "removed"):
            store.records(observation.attempt).recover_publications()
        if observation.action != "eligible":
            return observation
        attempt = observation.attempt
        record = store.cleanup_record(attempt)
        token = record["cleanup"] if record else uuid4().hex
        fields = dict(cleanup=token, operation=attempt["operation"], executions=attempt["executions"],
                      directories=observation.directories)
        progress = list(record.get("removed_paths", [])) if record else []
        pending = record.get("removing_path") if record else None

        def publish(state="removing"):
            store.publish(attempt, "cleanup.json", "cleanup", state=state,
                          removed_paths=progress, removing_path=pending, **fields)

        publish()
        for path, identity in observation.directories.items():
            store.validate_roots()
            if _files.exists(path):
                rechecked = observe(store, attempt, backend, backend_name, explicit=observation.explicit)
                if rechecked.action != "eligible":
                    observation.action, observation.reason = rechecked.action, rechecked.reason
                    observation.paths_error = rechecked.paths_error
                    return observation
                pending = path
                publish()
                observation.removing_path = path
                if _files.remove_directory(path, identity):
                    observation.removed_paths.append(path)
                progress.append(path)
                pending = None
                publish()
                observation.removing_path = None
            elif pending == path:
                # A crash after rmdir can precede parent synchronization. An
                # absent pathname alone does not establish durable removal.
                with _files.directory(Path(path).parent) as parent:
                    _files.sync_directory(parent)
                progress.append(path)
                pending = None
                publish()
        publish("removed")
        store.records(attempt).recover_publications()
        observation.action, observation.reason = "removed", "removed owned disposable work; results and bookkeeping preserved"
        return observation
    except BaseException as error:
        observation.action, observation.reason = "failed", str(error) or type(error).__name__
        raise
