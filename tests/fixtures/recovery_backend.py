"""Inject exceptional CLI transitions while submitting to real local workers.

The local worker protocol cannot pause a submitting CLI before tracking is
saved, reject one submission, or coordinate its two backend observations.
This fixture uses only gwf's existing backend plugin contract for those cases.
"""

import json
from pathlib import Path
import time

from gwf import Target
from gwf.backends.local import Client, LocalStatus, create_backend


def wait_for(predicate):
    deadline = time.monotonic() + 20
    while not predicate():
        if time.monotonic() > deadline:
            raise RuntimeError("timed out waiting for fixture coordination")
        time.sleep(0.05)


class RecoveryBackend:
    active = False

    def __init__(self, working_dir, port):
        if self.active:
            raise AssertionError("planning backend must close before submitting backend opens")
        self.work = Path(working_dir)
        self.port = int(port)
        self.options = json.loads((self.work / "injection.json").read_text())
        self.local = create_backend(working_dir, port=self.port)
        self.submitted = False

    @property
    def target_defaults(self):
        if self.options.get("capture_options"):
            return {"cores": 1, "memory": "1g"}
        return self.local.target_defaults

    def status(self, target):
        if self.options.get("hold_observation"):
            (self.work / "observation-held").touch()
            wait_for(lambda: (self.work / "observation-release").exists())
        return self.local.status(target)

    def submit(self, target, dependencies):
        if self.options.get("capture_options"):
            with (self.work / "submitted-options.jsonl").open("a") as stream:
                stream.write(json.dumps({"name": target.name, "options": target.options}) + "\n")
        if target.name == self.options.get("reject"):
            raise RuntimeError("injected submission failure")
        if target.name == self.options.get("hold_submission"):
            (self.work / "submission-held").touch()
            wait_for(lambda: (self.work / "submission-release").exists())
        self.local.submit(target, dependencies)
        self.submitted = True

    def get_tracked_id(self, target):
        return self.local.get_tracked_id(target)

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
                job_id = str(self.local.get_tracked_id(target))
                (self.work / "old.release").touch()
                def failed():
                    with Client.connect(port=self.port) as client:
                        return client.status()[job_id] == LocalStatus.FAILED
                wait_for(failed)
                (self.work / "fail-after-release").unlink()
                (self.work / "transition-done").touch()
            self.local.__exit__(*exc)
        finally:
            type(self).active = False


setup = (RecoveryBackend, -1)
