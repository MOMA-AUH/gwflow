"""Inject exceptional CLI transitions while submitting to real local workers.

The local worker protocol cannot pause a submitting CLI before tracking is
saved, reject one submission, or coordinate its two backend observations.
This fixture uses only gwf's existing backend plugin contract for those cases.
"""

import json
import os
from pathlib import Path
import shlex
import sys
import time

from gwf import Target
from gwf.backends import BackendStatus
from gwf.backends.base import TrackingBackend
from gwf.backends.local import Client, LocalStatus, LocalOps


def wait_for(predicate):
    deadline = time.monotonic() + 20
    while not predicate():
        if time.monotonic() > deadline:
            raise RuntimeError("timed out waiting for fixture coordination")
        time.sleep(0.05)


class RecoveryBackend(TrackingBackend):
    active = False

    def __init__(self, working_dir, port):
        if self.active:
            raise AssertionError("planning backend must close before submitting backend opens")
        self.work = Path(working_dir)
        self.port = int(port)
        self.options = json.loads((self.work / "injection.json").read_text())
        super().__init__(working_dir, name="local", ops=LocalOps(working_dir, "localhost", self.port, {}))
        self.submitted = False

    @property
    def target_defaults(self):
        if self.options.get("reject_before_admission"):
            raise RuntimeError("injected pre-admission resource rejection")
        if self.options.get("capture_options"):
            return {"cores": 1, "memory": "1g"}
        return super().target_defaults

    def status(self, target):
        for prefix, state in self.options.get("job_states", {}).items():
            if target.name.startswith(prefix + "__"):
                return BackendStatus[state]
        if self.options.get("queued_prefix") and target.name.startswith(self.options["queued_prefix"] + "__"):
            return BackendStatus.SUBMITTED
        if self.options.get("running_prefix") and target.name.startswith(self.options["running_prefix"] + "__"):
            return BackendStatus.RUNNING
        if self.options.get("hold_observation"):
            (self.work / "observation-held").touch()
            wait_for(lambda: (self.work / "observation-release").exists())
        return super().status(target)

    def submit(self, target, dependencies):
        if self.options.get("capture_options"):
            with (self.work / "submitted-options.jsonl").open("a") as stream:
                stream.write(json.dumps({"name": target.name, "options": target.options}) + "\n")
        if target.name == self.options.get("reject") or (self.options.get("reject_prefix") and target.name.startswith(self.options["reject_prefix"] + "__")):
            raise RuntimeError("injected submission failure")
        if target.name == self.options.get("hold_submission") or (self.options.get("hold_submission_prefix") and target.name.startswith(self.options["hold_submission_prefix"] + "__")):
            (self.work / "submission-held").touch()
            wait_for(lambda: (self.work / "submission-release").exists())
        if self.options.get("job_fault") and target.name.startswith(self.options["job_fault"] + "__"):
            original = shlex.split(target.spec)
            target.spec = shlex.join([sys.executable, str(self.work / "job_fault.py"), str(self.work), *original[1:]])
        super().submit(target, dependencies)
        self.submitted = True
        if self.options.get("lose_tracking") and target.name.startswith(self.options["lose_tracking"] + "__"):
            os._exit(93)
        if self.options.get("lose_ack") and target.name.startswith(self.options["lose_ack"] + "__"):
            raise RuntimeError("injected lost acknowledgement after acceptance")

    def get_tracked_id(self, target):
        return super().get_tracked_id(target)

    def __enter__(self):
        type(self).active = True
        return self

    def __exit__(self, *exc):
        try:
            if self.submitted and self.options.get("hold_tracking"):
                (self.work / "tracking-held").touch()
                wait_for(lambda: (self.work / "tracking-release").exists())
            transition = self.options.get("fail_after_observation")
            if transition and not (self.work / "transition-done").exists():
                # Release an actually running job into failure after planning's
                # snapshot, before the CLI opens its submitting backend.
                target = Target(transition, [], [], {})
                job_id = str(super().get_tracked_id(target))
                (self.work / "old.release").touch()
                def failed():
                    with Client.connect(port=self.port) as client:
                        return client.status()[job_id] == LocalStatus.FAILED
                wait_for(failed)
                (self.work / "fail-after-release").unlink()
                (self.work / "transition-done").touch()
            super().__exit__(*exc)
        finally:
            type(self).active = False


setup = (RecoveryBackend, -1)
