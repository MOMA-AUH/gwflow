"""Generation-specific admission and recovery over gwf's pinned backends.

Only this module knows how gwf 2.1.1 persists job tracking. Inspecting evidence
is read-only; restoration happens under the frontend guard during submission
or cancellation requests.
"""

from dataclasses import dataclass
from contextlib import nullcontext
import json
import time
from uuid import uuid4

from gwf import Target
from gwf.backends import BackendStatus
from gwf.backends.base import TrackingBackend
from gwf.backends.local import LocalOps
from gwf.backends.slurm import SlurmOps
from gwf.exceptions import WorkflowError
from gwf.scheduling import submit_backend

from . import _observations
from .activity import activity as report_activity
from ._frontend import observation_deadline
from .diagnostics import report_error
from .lifecycle import _uuid, ordered_targets, target_dependencies
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


@dataclass
class SubmissionOutcome:
    """What this frontend invocation knows about one planned backend call."""

    task: str
    local: str
    submission: str | None = None
    entered: bool = False
    confirmed: bool = False
    job_id: str | int | None = None
    attempt: str | None = None
    failed: bool = False


@dataclass
class RecoveryOutcome:
    """Invocation receipt retained before a resolution write can fail."""

    submission: str
    confirmed: bool = False


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
        return sorted(attempt["executions"])
    return ["gwflow_prepare", *target_dependencies(attempt["structure"], local)]


def _valid_dependencies(attempt, local, dependencies):
    if not isinstance(dependencies, list) or len(dependencies) != len(predecessors(attempt, local)):
        return False
    for dependency, expected in zip(dependencies, predecessors(attempt, local)):
        if (not isinstance(dependency, dict) or set(dependency) != {"local", "job", "admission", "submission", "backend"}
                or dependency["local"] != expected or not _uuid(dependency["admission"])
                or not _known_job(attempt, dependency["job"], expected)
                or dependency["submission"] != f"{dependency['job']}__{dependency['admission']}"
                or not isinstance(dependency["backend"], dict)):
            return False
    return True


def intent_path(attempt, local):
    generation = attempt["jobs"][local].rsplit("__", 1)[-1]
    return f"submissions/{generation}/{local}-intent.json"


def _known_job(attempt, job, local=None):
    if not isinstance(job, str):
        return False
    prefix, _, generation = job.rpartition("__")
    names = [local] if local is not None else lifecycle_jobs(attempt["executions"])
    return _uuid(generation) and prefix in {f"{attempt['task']}__{name}" for name in names}


def read_intent(store, attempt, local):
    path = intent_path(attempt, local)
    record = store.read(attempt, path, "submission-intent", **store.job_identity(attempt, local))
    if record is None:
        if store.exists(attempt, path):
            raise WorkflowError(f"Malformed submission intent for {attempt['task']}__{local}")
        for previous in recorded_intents(store, attempt):
            if previous["job"] == attempt["jobs"][local]:
                raise WorkflowError(f"unresolved submission: {previous['submission']}; current intent is missing")
        return None
    admission = record.get("admission")
    if (not _uuid(admission) or record.get("submission") != f"{attempt['jobs'][local]}__{admission}"
            or record.get("producers") != (attempt["producers"] if local == "gwflow_prepare" else {})
            or not isinstance(record.get("backend"), dict) or not _valid_dependencies(attempt, local, record.get("dependencies"))
            or store.read(attempt, f"admissions/{admission}/intent.json", "submission-intent") != record):
        raise WorkflowError(f"Malformed submission intent for {attempt['task']}__{local}")
    return record


def recorded_intents(store, attempt):
    """Read archived submission identities, including replaced retry admissions."""
    return [_archived_intent(store, attempt, token)
            for token in store.records(attempt).names("admissions")]


def _archived_intent(store, attempt, token):
    if not _uuid(token):
        raise WorkflowError("Malformed admission archive")
    record = store.read(attempt, f"admissions/{token}/intent.json", "submission-intent")
    if (record is None or record.get("admission") != token
            or not _known_job(attempt, record.get("job"))
            or record.get("submission") != f"{record['job']}__{token}"
            or not isinstance(record.get("backend"), dict)):
        raise WorkflowError(f"unresolved submission: admission {token} has unreadable history")
    return record


def identity(intent):
    return {key: intent[key] for key in ("job", "admission", "submission", "backend")}


def read_recovery(store, attempt, intent):
    path = f"recoveries/{intent['admission']}/recovery.json"
    record = store.read(attempt, path, "operator-recovery", **identity(intent))
    if record is None and not store.exists(attempt, path):
        return None
    if (record is None or record.get("assertion") != "whole-task-inactive"
            or not _uuid(record.get("recovery")) or type(record.get("asserted_at_ns")) is not int
            or record["asserted_at_ns"] <= 0):
        raise WorkflowError(f"Malformed operator recovery for {intent['submission']}")
    return record


def resolve_submissions(store, attempt, observations):
    active = [item.submission for item in observations
              if item.backend_state in (BackendStatus.SUBMITTED, BackendStatus.RUNNING)]
    if active:
        raise WorkflowError("Recovery requires whole-Task inactivity; queued or running submissions: " + ", ".join(active))
    unresolved = [item for item in observations if item.state == "uncertain"]
    recovery, asserted_at = uuid4().hex, time.time_ns()
    for item in unresolved:
        outcome = RecoveryOutcome(item.submission)
        yield outcome
        store.publish(attempt, f"recoveries/{item.intent['admission']}/recovery.json", "operator-recovery",
                      **identity(item.intent), recovery=recovery, asserted_at_ns=asserted_at,
                      assertion="whole-task-inactive", observed_job_id=item.job_id,
                      observed_backend_state=item.backend_state.name)
        outcome.confirmed = True


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


def dependencies_current(attempt, intent):
    return all(item["job"] == attempt["jobs"][item["local"]] for item in intent["dependencies"])


def require_dependencies(store, attempt, intent):
    if not dependencies_current(attempt, intent):
        raise WorkflowError("Scheduled dependency execution generation was replaced")
    for dependency in intent["dependencies"]:
        previous = store.read(attempt, f"admissions/{dependency['admission']}/intent.json", "submission-intent")
        expected = {key: value for key, value in dependency.items() if key != "local"}
        if previous is None or identity(previous) != expected:
            raise WorkflowError("Scheduled dependency generation does not match")
        if not succeeded(store, attempt, dependency["local"], previous):
            raise WorkflowError("Scheduled dependency lacks checked success evidence")


def observe(store, attempt, backend, name):
    """Inspect current admissions; backend=None uses only recorded evidence."""
    # Resolve the selected admissions before reuse: the attempt's execution
    # identity alone does not identify a resubmission of the same job.
    intents = {local: read_intent(store, attempt, local)
               for local in lifecycle_jobs(ordered_targets(attempt["structure"]))}
    key = (store, id(backend), name, json.dumps(backend_identity(backend, name), sort_keys=True),
           json.dumps(attempt, sort_keys=True), json.dumps(intents, sort_keys=True))
    return _observations.reuse("jobs", key, lambda: _observe(store, attempt, backend, name, intents))


def observe_submissions(store, attempt, backend, name, *, tolerate_state_errors=False):
    """Inspect all archived admissions, including superseded retry submissions."""
    observations = []
    for intent in recorded_intents(store, attempt):
        local = intent["job"].removeprefix(attempt["task"] + "__").rsplit("__", 1)[0]
        with observation_deadline() if tolerate_state_errors else nullcontext():
            observations.extend(_observe(store, attempt, backend, name, {local: intent},
                                         tolerate_state_errors=tolerate_state_errors).values())
    return observations


def observe_cancellation(store, attempt, local, backend, name):
    """Resolve one current admission even when its live state is unavailable."""
    intent = read_intent(store, attempt, local)
    return _observe(store, attempt, backend, name, {local: intent}, tolerate_state_errors=True)[local]


def cancellation_diagnostic_identity(store, attempt, local):
    """Retain valid saved identity after an observation fails, never authorize a request."""
    item = JobObservation(local, "uncertain")
    try:
        item.intent = read_intent(store, attempt, local)
        if item.intent is not None:
            ack = store.read(attempt, f"admissions/{item.intent['admission']}/ack.json",
                             "submission-ack", **identity(item.intent))
            if ack is not None:
                item.job_id = ack.get("job_id")
    except Exception:
        # The caller already reports the observation failure. Preserve only
        # the records validated so far, without obscuring that original error.
        pass
    return item


def superseded_activity(store, attempt, backend, name):
    """Recovery never overrides later activity, including after a retry."""
    active = []
    for token in store.records(attempt).names("recoveries"):
        intent = _archived_intent(store, attempt, token)
        local = intent["job"].removeprefix(attempt["task"] + "__").rsplit("__", 1)[0]
        if intent == read_intent(store, attempt, local):
            continue
        item = _observe(store, attempt, backend, name, {local: intent})[local]
        if item.state in ("active", "uncertain"):
            active.append(item)
    return active


def activity(store, attempt, backend, name):
    current = [item for item in observe(store, attempt, backend, name).values()
               if item.state in ("active", "uncertain")]
    return current + superseded_activity(store, attempt, backend, name)


def _observe(store, attempt, backend, name, intents, *, tolerate_state_errors=False):
    observations = {}
    for local, intent in intents.items():
        if intent is None:
            observations[local] = JobObservation(local, "pending")
            continue
        token = intent["admission"]
        ack = store.read(attempt, f"admissions/{token}/ack.json", "submission-ack", **identity(intent))
        rejection = store.read(attempt, f"admissions/{token}/rejected.json", "submission-rejected", **identity(intent))
        outcome = store.read(attempt, f"admissions/{token}/outcome.json", "job-outcome", **identity(intent))
        recovery = None
        checked = succeeded(store, attempt, local, intent)
        target = Target(intent["submission"], [], [], {})
        tracked_id, state = None, BackendStatus.UNKNOWN
        same_backend = backend is not None and intent["backend"] == backend_identity(backend, name)
        if same_backend:
            tracked_id = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
        saved_id = ack.get("job_id") if ack else None
        mismatch = tracked_id is not None and saved_id is not None and tracked_id != saved_id
        job_id = saved_id if saved_id is not None else tracked_id
        if same_backend:
            try:
                state = backend.status(target)
                if tracked_id is None and job_id is not None and isinstance(backend, TrackingBackend):
                    with report_activity("Waiting for scheduler..."):
                        state = backend.ops.get_job_states([job_id]).get(job_id, BackendStatus.UNKNOWN)
            except Exception as error:
                if not tolerate_state_errors:
                    recovery = read_recovery(store, attempt, intent)
                    if recovery is None:
                        raise
                report_error(error, f"Observing submission {intent['submission']}")
                state = BackendStatus.UNKNOWN
        if mismatch and recovery is None:
            recovery = read_recovery(store, attempt, intent)
        if mismatch and recovery is None:
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
            recovery = recovery or read_recovery(store, attempt, intent)
            status = "failed" if recovery is not None else "uncertain"
        observations[local] = JobObservation(local, status, intent, job_id,
                                             saved_id is None and job_id is not None and not mismatch, state)
    return observations


def acknowledge(store, attempt, intent, job_id):
    store.publish(attempt, f"admissions/{intent['admission']}/ack.json", "submission-ack",
                  **identity(intent), job_id=job_id)


def consumer_activity(store, backend, name, *, producers=None):
    """Recorded bindings protect producers even when a consumer is not loaded."""
    consumers = {}
    for attempt in store.recorded_tasks():
        bindings = {producer: expected for producer, expected in attempt["producers"].items()
                    if producers is None or producer in producers}
        if not bindings:
            continue
        active = activity(store, attempt, backend, name)
        if active:
            for producer, expected in bindings.items():
                consumers.setdefault((producer, expected), {})[attempt["task"]] = active
    return consumers


@dataclass
class ReplacementActivity:
    submissions: list
    consumers: dict

    @property
    def reason(self):
        if self.submissions:
            return "Unresolved or active work blocks replacement: " + ", ".join(self.submissions)
        if self.consumers:
            return "Active consumers block replacement: " + ", ".join(self.consumers)
        return None


def replacement_activity(store, attempt, backend, name, consumers):
    """Observe the attempt and any previous results protected by initialization."""
    observed = ReplacementActivity([], {})
    checked = set()
    while attempt is not None:
        if attempt["attempt"] in checked:
            raise WorkflowError("Cyclic previous-attempt initialization evidence")
        checked.add(attempt["attempt"])
        observed.submissions.extend(item.submission for item in activity(store, attempt, backend, name))
        observed.consumers.update(consumers.get((attempt["task"], attempt["attempt"]), {}))
        attempt = store.initialization_previous(attempt) if store.read(attempt, "ready.json", "ready") is None else None
    return observed


def restore_tracking(backend, name, observations):
    """Restore known IDs only during run, so gwf can encode dependency IDs."""
    if isinstance(backend, TrackingBackend):
        for observation in observations.values():
            if (observation.job_id is not None and observation.state != "uncertain"
                    and observation.intent["backend"] == backend_identity(backend, name)):
                backend._tracked_jobs[observation.submission] = observation.job_id


def require_cancellation_identity(backend, name, observation):
    """Cancellation needs a known job identity, independently of live state."""
    if observation.intent["backend"] != backend_identity(backend, name):
        raise WorkflowError("recorded backend does not match the selected backend")
    if observation.job_id is None:
        raise WorkflowError("no recorded or tracked backend job ID")
    target = Target(observation.submission, [], [], {})
    tracked = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
    if tracked is not None and tracked != observation.job_id:
        raise WorkflowError("conflicting backend job IDs")


def request_cancellation(backend, name, observation):
    """Use verified admission identity without recording any inactivity claim."""
    require_cancellation_identity(backend, name, observation)
    if isinstance(backend, TrackingBackend):
        backend._tracked_jobs[observation.submission] = observation.job_id
    backend.cancel(Target(observation.submission, [], [], {}))


def require_retry_inactive(observations, replaced):
    conflicting = [local for local in replaced if observations[local].state == "active"]
    finishing = observations["gwflow_complete"]
    if finishing.state == "active" and finishing.backend_state != BackendStatus.SUBMITTED:
        conflicting.append("gwflow_complete")
    if conflicting:
        raise WorkflowError("Active dependent work blocks retry: " + ", ".join(conflicting))


def new_intent(store, attempt, local, dependencies, backend, name):
    token = uuid4().hex
    fields = {**store.job_identity(attempt, local), "admission": token,
              "submission": f"{attempt['jobs'][local]}__{token}",
              "backend": backend_identity(backend, name), "dependencies": dependencies}
    fields["producers"] = attempt["producers"] if local == "gwflow_prepare" else {}
    store.publish(attempt, f"admissions/{token}/intent.json", "submission-intent", **fields)
    store.publish(attempt, intent_path(attempt, local), "submission-intent", **fields)
    return read_intent(store, attempt, local)


class _AdmissionCall:
    """Identify gwf preflight errors without interpreting backend exceptions."""

    def __init__(self, backend, accepted, outcome):
        self.backend = backend
        self.accepted = accepted
        self.outcome = outcome

    @property
    def target_defaults(self):
        return self.backend.target_defaults

    def submit(self, target, dependencies):
        self.outcome.entered = True
        result = self.backend.submit(target, dependencies)
        self.outcome.confirmed = True
        job_id = self.backend.get_tracked_id(target) if hasattr(self.backend, "get_tracked_id") else None
        self.outcome.job_id = job_id
        self.accepted(job_id)
        return result


def submit(store, attempt, intent, target, dependencies, backend, hashes, outcome):
    call = _AdmissionCall(backend, lambda job_id: acknowledge(store, attempt, intent, job_id), outcome)
    try:
        submit_backend(target, dependencies, call, hashes)
    except BaseException:
        if not outcome.entered:
            store.publish(attempt, f"admissions/{intent['admission']}/rejected.json", "submission-rejected", **identity(intent))
        raise
    job_id = backend.get_tracked_id(target) if hasattr(backend, "get_tracked_id") else None
    return JobObservation("", "active", intent, job_id)
