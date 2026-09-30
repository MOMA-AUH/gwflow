"""Observe and durably remove eligible owned disposable Task storage."""

from dataclasses import dataclass, field
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


def observe(store, attempt, backend, backend_name, *, explicit=False):
    observation = CleanupObservation(attempt, "keep", "incomplete computation or results", explicit=explicit)
    try:
        store.validate_roots()
        if not explicit and store.recorded_current(attempt["task"])["attempt"] != attempt["attempt"]:
            observation.reason = "superseded attempt; default cleanup keeps older work"
            return observation
        jobs = admission.observe(store, attempt, backend, backend_name)
        uncertain = [job.submission for job in jobs.values() if job.state == "uncertain"]
        active = [job.submission for job in jobs.values() if job.state == "active"]
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
            if _files.exists(path) and _files.identity(path) != identity:
                raise WorkflowError(f"Disposable directory ownership changed: {path}")
        if record is not None and record["directories"] != observation.directories:
            raise WorkflowError("Cleanup ownership evidence changed")
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
    return observation


def remove(store, observation, backend, backend_name):
    checked = observe(store, observation.attempt, backend, backend_name, explicit=observation.explicit)
    if checked.action != "eligible":
        return checked
    attempt = checked.attempt
    record = store.cleanup_record(attempt)
    token = record["cleanup"] if record else uuid4().hex
    fields = dict(cleanup=token, operation=attempt["operation"], executions=attempt["executions"],
                  directories=checked.directories)
    store.publish(attempt, "cleanup.json", "cleanup", state="removing", **fields)
    for path, identity in checked.directories.items():
        store.validate_roots()
        if _files.exists(path):
            rechecked = observe(store, attempt, backend, backend_name, explicit=observation.explicit)
            if rechecked.action != "eligible":
                return rechecked
            _files.remove_directory(path, identity)
    store.publish(attempt, "cleanup.json", "cleanup", state="removed", **fields)
    checked.action, checked.reason = "removed", "removed owned disposable work; results and bookkeeping preserved"
    return checked
