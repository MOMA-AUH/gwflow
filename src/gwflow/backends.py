"""Keep gwf backend discovery while requiring safe managed local admissions."""

from pathlib import Path
import socket
import time

from gwf.backends import create_backend as gwf_create_backend
from gwf.backends.base import TrackingBackend
from gwf.backends.exceptions import BackendError
from gwf.backends.local import Client, LocalOps, LocalStatus, STATUS_MAP, TARGET_DEFAULTS, get_socket_path
from gwf.exceptions import WorkflowError

from .inspection_errors import InspectionBlocker
from ._local_termination import TERMINAL_STATES, TerminationRecords


class LocalWorkerUnavailable(BackendError):
    """Connection establishment failed; this says nothing about job activity."""


class LocalTerminationUnresolved(InspectionBlocker):
    def __init__(self, detail=None):
        message = ("Local worker cannot confirm computation descendants have stopped; "
                   "establish inactivity before using gwflow recover. Use gwflow workers for new work.")
        super().__init__("submission_unresolved", f"{detail}. {message}" if detail else message)


def local_termination_state(working_dir, submission):
    """Read exact-Submission inactivity evidence independently of live workers."""
    try:
        state = TerminationRecords(working_dir).read(submission)
    except (OSError, WorkflowError) as error:
        raise LocalTerminationUnresolved(f"Cannot read local termination evidence: {error}") from error
    if state not in TERMINAL_STATES:
        raise LocalTerminationUnresolved()
    return STATUS_MAP[state]


class ManagedLocalOps(LocalOps):
    def __init__(self, working_dir):
        self.working_dir = str(Path(working_dir).resolve())
        self.socket_path = get_socket_path(self.working_dir)
        self.target_defaults = TARGET_DEFAULTS
        self.managed = None
        # Own establishment rather than inheriting gwf's exponential retry
        # policy and its guidance for unmanaged workers. Keep its wire client.
        for attempt in range(3):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.settimeout(1)
                sock.connect(str(self.socket_path))
                sock.settimeout(None)
                self._client = Client.from_socket(sock)
                break
            except OSError:
                sock.close()
                if attempt < 2:
                    time.sleep(0.25)
            except BaseException:
                sock.close()
                raise
        else:
            raise LocalWorkerUnavailable(
                f"Local backend could not connect to the scheduler for {self.working_dir} "
                f"at {self.socket_path} after 3 attempts. Start or check gwflow workers in the workflow directory. "
                "Job activity is unknown; no inactivity is established.")

    def get_job_states(self, tracked_jobs):
        self._client.send("get_task_states")
        kind, response = self._client.recv()
        if kind != "task_states":
            raise BackendError("Invalid local worker state response")
        names = response.get("gwflow_submissions")
        self.managed = (response.get("gwflow_process_tree") == 1
                        and isinstance(names, dict) and response.get("gwflow_termination_records") == 1)
        self.submission_names = dict(names) if self.managed else {}
        states = {key: LocalStatus[value] for key, value in response["tasks"].items()}
        self._last_states = states
        self.known_jobs = set(states)
        self.unresolved = {key for key, state in states.items()
                           if state == LocalStatus.UNKNOWN or
                           (not self.managed and state in (LocalStatus.CANCELLED, LocalStatus.KILLED, LocalStatus.FAILED))}
        return {key: STATUS_MAP[state] for key, state in states.items() if key in tracked_jobs}

    def _ensure_observed(self):
        # Empty tracking and cached states need not trigger an upstream query.
        # Explicitly establish capabilities/identity when first needed, sharing
        # any observation already made during TrackingBackend initialization.
        if self.managed is None:
            self.get_job_states(())

    def observe_submission(self, job_id, *, submission):
        """Observe this submission, requiring termination evidence for inactivity."""
        self._ensure_observed()
        if self.managed and self.submission_names.get(job_id) == submission:
            if job_id in self.unresolved:
                raise LocalTerminationUnresolved()
            return STATUS_MAP[self._last_states[job_id]]
        if not self.managed and job_id in self.known_jobs:
            if self._last_states[job_id] in (LocalStatus.SUBMITTED, LocalStatus.RUNNING):
                return STATUS_MAP[self._last_states[job_id]]
        # Output success precedes descendant cleanup. Only a durable receipt
        # for this exact submission can replace an expired live identity.
        try:
            return local_termination_state(self.working_dir, submission)
        except LocalTerminationUnresolved:
            if self.managed and job_id in self.known_jobs:
                raise LocalTerminationUnresolved("Local job ID belongs to a different submission") from None
            raise

    def require_cancellation_identity(self, job_id, submission):
        self._ensure_observed()
        if not self.managed:
            raise BackendError("Local workers cannot verify cancellation identity. "
                               "Stop old computation independently, then restart with gwflow workers.")
        if self.submission_names.get(job_id) != submission:
            raise LocalTerminationUnresolved("Local job ID is absent or belongs to a different submission")

    def require_managed(self):
        self._ensure_observed()
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
    if settings:
        raise BackendError("Local workers are selected by workflow directory; remove retired "
                           "backend.local settings (including host/port). Use gwflow workers in that directory.")
    ops = ManagedLocalOps(working_dir)
    try:
        return ManagedLocalBackend(working_dir, name="local", ops=ops)
    except BaseException:
        ops.close()
        raise
