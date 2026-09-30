"""Generation-specific admission and recovery over gwf's pinned backends.

Only this module knows how gwf 2.1.1 persists job tracking. Inspecting evidence
is read-only; restoration happens under the frontend guard during submission.
"""

from dataclasses import dataclass
import os
from uuid import uuid4

from gwf import Target
from gwf.backends import BackendStatus
from gwf.backends.base import TrackingBackend
from gwf.backends.local import LocalOps
from gwf.backends.slurm import SlurmOps
from gwf.exceptions import WorkflowError
from gwf.scheduling import submit_backend

from . import _files
from .lifecycle import _uuid
from .workflow import lifecycle_jobs


@dataclass
class JobObservation:
    local: str
    state: str
    intent: dict | None = None
    job_id: str | int | None = None
    reconcile: bool = False
    backend_state: BackendStatus = BackendStatus.UNKNOWN

    @property
    def submission(self):
        return self.intent["submission"] if self.intent else None


def backend_identity(backend, name):
    if isinstance(backend, TrackingBackend):
        if isinstance(backend.ops, LocalOps):
            return {"name": "local", "host": backend.ops.host, "port": backend.ops.port}
        if isinstance(backend.ops, SlurmOps):
            return {"name": "slurm"}
    return {"name": name}


def predecessors(attempt, local):
    if local == "gwflow_prepare":
        return []
    if local == "gwflow_complete":
        return list(attempt["executions"])
    return ["gwflow_prepare"]


def _valid_dependencies(attempt, local, dependencies):
    if not isinstance(dependencies, list) or len(dependencies) != len(predecessors(attempt, local)):
        return False
    for dependency, expected in zip(dependencies, predecessors(attempt, local)):
        if (not isinstance(dependency, dict) or set(dependency) != {"local", "job", "admission", "submission", "backend"}
                or dependency["local"] != expected or not _uuid(dependency["admission"])
                or dependency["job"] != attempt["jobs"][expected]
                or dependency["submission"] != f"{dependency['job']}__{dependency['admission']}"
                or not isinstance(dependency["backend"], dict)):
            return False
    return True


def read_intent(store, attempt, local):
    path = f"submissions/{local}-intent.json"
    record = store.read(attempt, path, "submission-intent", **store.job_identity(attempt, local))
    if record is None:
        if _files.exists(store.attempt_dir(attempt) / path):
            raise WorkflowError(f"Malformed submission intent for {attempt['task']}__{local}")
        archive = store.attempt_dir(attempt) / "admissions"
        if _files.exists(archive):
            with _files.directory(archive) as directory:
                tokens = os.listdir(directory)
            for token in tokens:
                if not _uuid(token):
                    raise WorkflowError("Malformed admission archive")
                previous = store.read(attempt, f"admissions/{token}/intent.json", "submission-intent")
                if (previous is None or previous.get("admission") != token
                        or previous.get("job") not in attempt["jobs"].values()):
                    raise WorkflowError(f"unresolved submission: admission {token} has unreadable history")
                if previous["job"] == attempt["jobs"][local]:
                    raise WorkflowError(f"unresolved submission: {previous.get('submission', local)}; current intent is missing")
        return None
    admission = record.get("admission")
    if (not _uuid(admission) or record.get("submission") != f"{attempt['jobs'][local]}__{admission}"
            or not isinstance(record.get("backend"), dict) or not _valid_dependencies(attempt, local, record.get("dependencies"))
            or store.read(attempt, f"admissions/{admission}/intent.json", "submission-intent") != record):
        raise WorkflowError(f"Malformed submission intent for {attempt['task']}__{local}")
    return record


def identity(intent):
    return {key: intent[key] for key in ("job", "admission", "submission", "backend")}


def success_evidence(store, attempt, local, intent):
    token = intent["admission"]
    if local == "gwflow_prepare":
        record = store.input_baseline(attempt)
    elif local == "gwflow_complete":
        record = store.read(attempt, "completion.json", "completion", operation=attempt["operation"])
    else:
        record = store.checked_target(attempt, local, check_files=False)
    return record is not None and record.get("admission") == token


def succeeded(store, attempt, local, intent):
    outcome = store.read(attempt, f"admissions/{intent['admission']}/outcome.json", "job-outcome", **identity(intent))
    if outcome is not None and outcome.get("state") == "complete":
        return True
    try:
        return success_evidence(store, attempt, local, intent)
    except (WorkflowError, OSError):
        return False


def require_dependencies(store, attempt, intent):
    for dependency in intent["dependencies"]:
        previous = store.read(attempt, f"admissions/{dependency['admission']}/intent.json", "submission-intent")
        expected = {key: value for key, value in dependency.items() if key != "local"}
        if previous is None or identity(previous) != expected:
            raise WorkflowError("Scheduled dependency generation does not match")
        if not succeeded(store, attempt, dependency["local"], previous):
            raise WorkflowError("Scheduled dependency lacks checked success evidence")


def observe(store, attempt, backend, name):
    observations = {}
    for local in lifecycle_jobs(attempt["executions"]):
        intent = read_intent(store, attempt, local)
        if intent is None:
            observations[local] = JobObservation(local, "pending")
            continue
        token = intent["admission"]
        ack = store.read(attempt, f"admissions/{token}/ack.json", "submission-ack", **identity(intent))
        rejection = store.read(attempt, f"admissions/{token}/rejected.json", "submission-rejected", **identity(intent))
        outcome = store.read(attempt, f"admissions/{token}/outcome.json", "job-outcome", **identity(intent))
        checked = succeeded(store, attempt, local, intent)
        target = Target(intent["submission"], [], [], {})
        tracked_id, state = None, BackendStatus.UNKNOWN
        same_backend = intent["backend"] == backend_identity(backend, name)
        if same_backend:
            tracked_id = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
            state = backend.status(target)
        saved_id = ack.get("job_id") if ack else None
        mismatch = tracked_id is not None and saved_id is not None and tracked_id != saved_id
        job_id = saved_id if saved_id is not None else tracked_id
        if same_backend and tracked_id is None and job_id is not None and isinstance(backend, TrackingBackend):
            state = backend.ops.get_job_states([job_id]).get(job_id, BackendStatus.UNKNOWN)
        if mismatch:
            status = "uncertain"
        elif state in (BackendStatus.SUBMITTED, BackendStatus.RUNNING):
            status = "active"
        elif checked:
            status = "complete"
        elif rejection is not None:
            status = "pending"
        elif outcome and outcome.get("state") == "failed":
            status = "failed"
        elif state == BackendStatus.FAILED:
            status = "failed"
        elif state == BackendStatus.CANCELLED:
            status = "cancelled"
        elif state == BackendStatus.COMPLETED:
            # Scheduler exit status cannot stand in for checked file evidence.
            status = "failed"
        else:
            status = "uncertain"
        observations[local] = JobObservation(local, status, intent, job_id,
                                             saved_id is None and job_id is not None and not mismatch, state)
    return observations


def acknowledge(store, attempt, intent, job_id):
    store.publish(attempt, f"admissions/{intent['admission']}/ack.json", "submission-ack",
                  **identity(intent), job_id=job_id)


def restore_tracking(backend, name, observations):
    """Restore known IDs only during run, so gwf can encode dependency IDs."""
    if isinstance(backend, TrackingBackend):
        for observation in observations.values():
            if (observation.job_id is not None and observation.state != "uncertain"
                    and observation.intent["backend"] == backend_identity(backend, name)):
                backend._tracked_jobs[observation.submission] = observation.job_id


def new_intent(store, attempt, local, dependencies, backend, name):
    token = uuid4().hex
    fields = {**store.job_identity(attempt, local), "admission": token,
              "submission": f"{attempt['jobs'][local]}__{token}",
              "backend": backend_identity(backend, name), "dependencies": dependencies}
    store.publish(attempt, f"admissions/{token}/intent.json", "submission-intent", **fields)
    store.publish(attempt, f"submissions/{local}-intent.json", "submission-intent", **fields)
    return read_intent(store, attempt, local)


class _AdmissionCall:
    """Identify gwf preflight errors without interpreting backend exceptions."""

    def __init__(self, backend, accepted):
        self.backend = backend
        self.accepted = accepted
        self.entered = False

    @property
    def target_defaults(self):
        return self.backend.target_defaults

    def submit(self, target, dependencies):
        self.entered = True
        result = self.backend.submit(target, dependencies)
        job_id = self.backend.get_tracked_id(target) if hasattr(self.backend, "get_tracked_id") else None
        self.accepted(job_id)
        return result


def submit(store, attempt, intent, target, dependencies, backend, hashes):
    call = _AdmissionCall(backend, lambda job_id: acknowledge(store, attempt, intent, job_id))
    try:
        submit_backend(target, dependencies, call, hashes)
    except BaseException:
        if not call.entered:
            store.publish(attempt, f"admissions/{intent['admission']}/rejected.json", "submission-rejected", **identity(intent))
        raise
    job_id = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
    return JobObservation("", "active", intent, job_id)
