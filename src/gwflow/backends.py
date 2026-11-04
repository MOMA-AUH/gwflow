"""Keep gwf backend discovery while requiring safe managed local admissions."""

from gwf.backends import create_backend as gwf_create_backend
from gwf.backends.base import TrackingBackend
from gwf.backends.exceptions import BackendError
from gwf.backends.local import LocalOps, LocalStatus, STATUS_MAP

from .inspection_errors import InspectionBlocker


class LocalTerminationUnresolved(InspectionBlocker):
    def __init__(self, detail=None):
        message = ("Local worker cannot confirm computation descendants have stopped; "
                   "establish inactivity before using gwflow recover. Use gwflow workers for new work.")
        super().__init__("submission_unresolved", f"{detail}. {message}" if detail else message)


class ManagedLocalOps(LocalOps):
    def get_job_states(self, tracked_jobs):
        self._client.send("get_task_states")
        kind, response = self._client.recv()
        if kind != "task_states":
            raise BackendError("Invalid local worker state response")
        names = response.get("gwflow_submissions")
        termination = response.get("gwflow_terminations")
        self.managed = (response.get("gwflow_process_tree") == 1
                        and isinstance(names, dict) and isinstance(termination, dict))
        self.submission_names = {int(key): value for key, value in names.items()} if self.managed else {}
        self.termination = termination if self.managed else {}
        states = {int(key): LocalStatus[value] for key, value in response["tasks"].items()}
        self._last_states = states
        self.known_jobs = set(states)
        self.unresolved = {key for key, state in states.items()
                           if state == LocalStatus.UNKNOWN or
                           (not self.managed and state in (LocalStatus.CANCELLED, LocalStatus.KILLED, LocalStatus.FAILED))}
        return {key: STATUS_MAP[state] for key, state in states.items() if key in tracked_jobs}

    def observe_submission(self, job_id, *, submission):
        """Observe this submission, requiring termination evidence for inactivity."""
        if self.managed and self.submission_names.get(job_id) == submission:
            if job_id in self.unresolved:
                raise LocalTerminationUnresolved()
            return STATUS_MAP[self._last_states[job_id]]
        if not self.managed and job_id in self.known_jobs:
            if self._last_states[job_id] in (LocalStatus.SUBMITTED, LocalStatus.RUNNING):
                return STATUS_MAP[self._last_states[job_id]]
        # Output success precedes descendant cleanup. Only a durable receipt
        # for this exact submission can replace an expired live identity.
        terminal = self.termination.get(submission)
        if terminal in ("COMPLETED", "FAILED", "CANCELLED", "KILLED"):
            return STATUS_MAP[LocalStatus[terminal]]
        if self.managed and job_id in self.known_jobs:
            raise LocalTerminationUnresolved("Local job ID belongs to a different submission")
        raise LocalTerminationUnresolved()

    def require_cancellation_identity(self, job_id, submission):
        if not self.managed:
            raise BackendError("Local workers cannot verify cancellation identity. "
                               "Stop old computation independently, then restart with gwflow workers.")
        if self.submission_names.get(job_id) != submission:
            raise LocalTerminationUnresolved("Local job ID is absent or belongs to a different submission")

    def require_managed(self):
        if not self.managed:
            raise BackendError("Local workers do not confirm process-tree termination. "
                               "Stop old jobs and restart with gwflow workers before submitting managed work.")

    def submit_target(self, target, dependency_ids):
        self.require_managed()
        return super().submit_target(target, dependency_ids)


class ManagedLocalBackend(TrackingBackend):
    @property
    def target_defaults(self):
        # Reject unsupported workers in gwf's preflight, before the admission
        # call becomes uncertain and would need an operator recovery assertion.
        self.ops.require_managed()
        return super().target_defaults


def create_backend(name, working_dir, config):
    if name != "local":
        return gwf_create_backend(name, working_dir=working_dir, config=config)
    settings = config.get_namespace("backend.local")
    ops = ManagedLocalOps(working_dir=working_dir, target_defaults={},
                          **{"host": "localhost", "port": 12345, **settings})
    try:
        return ManagedLocalBackend(working_dir, name="local", ops=ops)
    except BaseException:
        ops.close()
        raise
