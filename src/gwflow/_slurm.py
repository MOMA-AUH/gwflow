"""Exact Submission observations and controller-filtered Slurm cancellation.

Keep the pinned gwf submission/log contract; numeric-only state caches are never
evidence. Scheduler snapshots are shared by every admission in an observation.
"""

import os
import re

from gwf.backends import BackendStatus
from gwf.backends.base import TrackingBackend
from gwf.backends.exceptions import BackendError
from gwf.backends.slurm import SlurmOps, SLURM_LONG_STATES, SLURM_JOB_STATES, TARGET_DEFAULTS
from gwf.backends.utils import call

from .inspection_errors import InspectionBlocker


class SlurmIdentityUnresolved(InspectionBlocker):
    def __init__(self, detail):
        super().__init__("submission_unresolved", "Slurm Submission identity unresolved: " + detail +
                         ". No inactivity is established; inspect the exact Submission and "
                         "establish whole-Task inactivity before using gwflow recover.")


class ManagedSlurmOps(SlurmOps):
    def __init__(self, working_dir, log_mode="full", accounting_enabled=True):
        super().__init__(working_dir, log_mode, accounting_enabled, TARGET_DEFAULTS)
        self.tracked_ids = set()
        self.queried_ids = set()
        self.accounting = {}
        self.queue = None
        self.query_error = None
        self.cancel_capable = False
        self.verified = {}

    def get_job_states(self, tracked_jobs):
        # TrackingBackend eagerly loads states without knowing Submission names.
        # Defer querying until admission supplies both parts of the identity.
        self.tracked_ids.update(tracked_jobs)
        return {}

    def _records(self, output):
        records: dict[str, set[tuple[str, BackendStatus]]] = {}
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = line.split("|")
            if not re.fullmatch(r"[1-9][0-9]*", fields[0]):
                # squeue also lists other users' arrays and heterogeneous jobs.
                # Ignore only recognizable out-of-scope IDs. A malformed live
                # identity must not disappear behind terminal accounting.
                if re.fullmatch(r"[1-9][0-9]*(?:_(?:[0-9]+|\[[0-9,:%-]+\])|\+[0-9]+)?"
                                r"(?:\.(?:[0-9]+|batch|extern))?", fields[0]):
                    continue
                raise BackendError("Malformed Slurm job identity in scheduler response")
            if len(fields) != 3:
                records.setdefault(fields[0], set()).add(("", BackendStatus.UNKNOWN))
                continue
            job_id, state, name = fields
            words = state.split()
            state = words[0] if words else ""
            value = (name, SLURM_JOB_STATES[SLURM_LONG_STATES.get(state, state)])
            records.setdefault(job_id, set()).add(value)
        return records

    def prepare_observations(self, job_ids):
        self.tracked_ids.update(job_id for job_id in job_ids
                                if isinstance(job_id, str) and re.fullmatch(r"[1-9][0-9]*", job_id))
        if self.query_error is not None:
            return
        try:
            if self.queue is None:
                self.queue = self._records(call("squeue", "--noheader", "--format=%i|%t|%j", "--all", "--local"))
            missing = sorted(self.tracked_ids - self.queried_ids)
            for start in range(0, len(missing), 1024):
                batch = missing[start:start + 1024]
                if self.accounting_enabled:
                    output = call("sacct", "--noheader", "--parsable2", "--format=JobIDRaw,State,JobName%1024",
                                  "--allocations", "--local", "--jobs", ",".join(batch))
                    self.accounting.update(self._records(output))
                self.queried_ids.update(batch)
        except (BackendError, OSError) as error:
            self.query_error = str(error)

    def observe_submission(self, job_id, *, submission):
        key = (job_id, submission)
        if key in self.verified:
            return self.verified[key]
        if not isinstance(job_id, str) or not re.fullmatch(r"[1-9][0-9]*", job_id):
            raise SlurmIdentityUnresolved("no supported numeric job ID")
        self.prepare_observations([job_id])
        if self.query_error is not None:
            raise SlurmIdentityUnresolved(self.query_error)
        queue = (self.queue or {}).get(job_id, set())
        accounting = self.accounting.get(job_id, set())
        records = queue | accounting
        if not records or any(name != submission for name, state in records):
            raise SlurmIdentityUnresolved(f"job {job_id} is absent or belongs to another Submission")
        # Live state supersedes accounting lag, but never a contradictory name.
        selected = queue or accounting
        if len(selected) != 1:
            raise SlurmIdentityUnresolved(f"conflicting scheduler records for job {job_id}")
        _, state = next(iter(selected))
        if state == BackendStatus.UNKNOWN:
            raise SlurmIdentityUnresolved(f"incomplete scheduler state for job {job_id}")
        self.verified[key] = state
        return state

    def require_cancellation_identity(self, job_id, submission):
        self.observe_submission(job_id, submission=submission)
        if not self.cancel_capable:
            version = call("scancel", "--version").strip()
            match = re.fullmatch(r"slurm (\d+)\.(\d+)\.\d+(?:[-+].*)?", version)
            if match is None or tuple(map(int, match.groups())) < (24, 11):
                raise BackendError("Safe Slurm cancellation requires scancel 24.11 or newer and a controller "
                                   "supporting filtered kill_jobs requests; update the deployment. No request sent.")
            self.cancel_capable = True

    def cancel_submission(self, job_id, submission):
        self.require_cancellation_identity(job_id, submission)
        # Prevent ambient SCANCEL_* options from switching to client filtering,
        # selecting another cluster, or changing the signal/selection semantics.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("SCANCEL_") and key != "SLURM_CLUSTERS"}
        call("scancel", "--verbose", "--ctld", f"--name={submission}", str(job_id), environ=environment)


class ManagedSlurmBackend(TrackingBackend):
    def _read_status_cache(self):
        return {}  # Upstream's numeric-ID terminal cache cannot establish identity.

    def status(self, target):
        return self.ops.observe_submission(self.get_tracked_id(target), submission=target.name)

    def cancel(self, target):
        self.ops.cancel_submission(self.get_tracked_id(target), target.name)


def create_slurm_backend(working_dir, config):
    return ManagedSlurmBackend(working_dir, name="slurm",
                               ops=ManagedSlurmOps(working_dir, **config.get_namespace("backend.slurm")))
