"""Keep gwf backend discovery while requiring safe managed local admissions."""

from gwf.backends import create_backend as gwf_create_backend
from gwf.backends.base import TrackingBackend
from gwf.backends.exceptions import BackendError
from gwf.backends.local import LocalOps, LocalStatus, STATUS_MAP

from .inspection_errors import InspectionBlocker


class LocalTerminationUnresolved(InspectionBlocker):
    def __init__(self):
        super().__init__("submission_unresolved",
                         "Local worker cannot confirm computation descendants have stopped; "
                         "establish inactivity before using gwflow recover. Use gwflow workers for new work.")


class ManagedLocalOps(LocalOps):
    def get_job_states(self, tracked_jobs):
        self._client.send("get_task_states")
        kind, response = self._client.recv()
        if kind != "task_states":
            raise BackendError("Invalid local worker state response")
        self.managed = response.get("gwflow_process_tree") == 1
        states = {int(key): LocalStatus[value] for key, value in response["tasks"].items()}
        self.known_jobs = set(states)
        self.unresolved = {key for key, state in states.items()
                           if state == LocalStatus.UNKNOWN or
                           (not self.managed and state in (LocalStatus.CANCELLED, LocalStatus.KILLED, LocalStatus.FAILED))}
        return {key: STATUS_MAP[state] for key, state in states.items() if key in tracked_jobs}

    def require_observation(self, job_id, *, completed):
        if (job_id in self.unresolved or
                (job_id is not None and job_id not in self.known_jobs and not completed)):
            raise LocalTerminationUnresolved()

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
