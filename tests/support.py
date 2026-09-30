"""Completion and reuse through the installed API and ordinary gwf CLI."""

from collections import Counter
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from gwf.backends.local import Client, LocalStatus


GWF = str(Path(sys.executable).with_name("gwf"))
FIXTURES = Path(__file__).parent / "fixtures"


class LocalBackendTestCase(unittest.TestCase):
    workers = 2

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow reuse ")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        (self.work / "input.txt").write_text("hello\n")
        (self.work / "extra.txt").write_text("extra\n")
        self.configure_workflow()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.config = {"backend": "local", "backend.local.port": self.port}
        self.configure()
        worker = subprocess.Popen(
            [GWF, "workers", "-n", str(self.workers), "-p", str(self.port)], cwd=self.work,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(self.stop_worker, worker)
        self.wait_for(lambda: self.worker_ready(worker))

    def worker_ready(self, worker):
        if worker.poll() is not None:
            self.fail(f"gwf workers exited: {worker.stderr.read()}")
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                return True
        except OSError:
            return False

    def stop_worker(self, worker):
        worker.terminate()
        try:
            worker.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate(timeout=10)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for local jobs")

    def configure(self, **options):
        self.config.update(options)
        (self.work / ".gwfconf.json").write_text(json.dumps(self.config))

    def state_backend(self, states):
        shutil.copy(FIXTURES / "state_backend.py", self.work)
        metadata = self.work / "state_backend-1.0.dist-info"
        metadata.mkdir(exist_ok=True)
        (metadata / "entry_points.txt").write_text("[gwf.backends]\nstate_fixture = state_backend:setup\n")
        (self.work / "backend-state.json").write_text(json.dumps(states))
        return {**os.environ, "PYTHONPATH": str(self.work)}

    def configure_workflow(self, **options):
        raise NotImplementedError("Tests supply the public workflow declaration")

    def cli(self, *args, success=True, env=None):
        result = subprocess.run(
            [GWF, *args], cwd=self.work, capture_output=True, text=True,
            timeout=30, env=env,
        )
        output = result.stdout + result.stderr
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        return output

    def finish(self):
        def done():
            with Client.connect(port=self.port) as client:
                states = client.status()
            self.assertFalse(any(state == LocalStatus.FAILED for state in states.values()), states)
            return states and all(state == LocalStatus.COMPLETED for state in states.values())
        self.wait_for(done)

    def run_complete(self):
        output = self.cli("run")
        self.finish()
        return output

    def trace(self):
        return Counter((self.work / "trace.txt").read_text().splitlines())

