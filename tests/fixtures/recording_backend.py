"""Record backend acceptances without launching jobs, using gwf's real tracking file."""

import os
from pathlib import Path
import time
from uuid import uuid4

from gwf.backends import BackendStatus
from gwf.backends.base import TrackingBackend


def hold(phase):
    gate = os.environ.get("GWFLOW_TEST_GATE")
    if gate and phase == os.environ.get("GWFLOW_TEST_PHASE"):
        gate = Path(gate)
        gate.with_suffix(".held").touch()
        deadline = time.monotonic() + 15
        while not gate.with_suffix(".release").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for " + phase)
            time.sleep(0.02)


class RecordingOps:
    target_defaults = {}

    def __init__(self):
        self.submitted = False

    def submit_target(self, target, dependency_ids):
        self.submitted = True
        with Path(os.environ["GWFLOW_TEST_ACCEPTANCES"]).open("a") as stream:
            stream.write(target.name + "\n")
        return uuid4().hex

    def get_job_states(self, ids):
        return {job_id: BackendStatus.SUBMITTED for job_id in ids}

    def close(self):
        if self.submitted:
            hold("tracking")


def backend(working_dir):
    # A fresh Task opens one backend for planning and a second after attempt
    # initialization for submission.
    hold("submission" if backend.opened else "planning")
    backend.opened = True
    return TrackingBackend(working_dir, name="recording", ops=RecordingOps())


backend.opened = False
setup = (backend, -1)
